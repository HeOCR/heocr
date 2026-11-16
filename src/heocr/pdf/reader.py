"""The PDF route: per-page text-layer reading through pypdfium2.

Characters and their boxes come from pdfium; line building and reading order are ours
(``heocr.pdf.lines``). pdfium's own text order is not used: it already reorders lines,
and that is the step this package owns.
"""

from __future__ import annotations

import ctypes
from collections import Counter
import math
import unicodedata
from dataclasses import dataclass, field

from heocr import repairs
from heocr.contract import Block, Document, Line, Page, Unread
from heocr.ocr import OcrUnavailable, block_ocr, engine_for, unavailable_reason
from heocr.pdf.classify import classify, object_stats, stream_signals, text_signals
from heocr.pdf.lines import Atom, PageLines, build_lines, paragraphs
from heocr.textflags import text_flags

TOOL = "heocr.pdf"
# Characters whose baseline angle has a sine above this are rotated text.
ROTATED_SIN = 0.1


@dataclass
class PageText:
    """What the text layer of one page holds."""

    atoms: list[Atom] = field(default_factory=list)
    chars_total: int = 0  # non-space characters drawn by the page's own text objects
    controls: int = 0
    invisible: int = 0
    lone_surrogates: int = 0
    rotated: int = 0  # characters drawn at an angle (vertical or rotated text), not read in D1
    # Loose boxes of every character in the layer (rotated and invisible ones included), top-left
    # origin, points: masked out of image regions before region OCR, so the layer's text is not read twice.
    drawn_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    fonts: dict[str, int] = field(default_factory=dict)


def _font_name(raw, textpage, index: int) -> str:
    buffer = ctypes.create_string_buffer(256)
    flags = ctypes.c_int(0)
    length = raw.FPDFText_GetFontInfo(textpage, index, buffer, 256, ctypes.byref(flags))
    if length <= 0:
        return ""
    return buffer.value.decode("utf-8", "replace")


def page_text(page, eth_fonts: frozenset[str] = frozenset()) -> PageText:
    """Characters with loose boxes, in page coordinates with the origin at the top left.

    ``eth_fonts`` names the fonts whose ð is a broken נ (``latin1_eth_nun``, decided per font
    by ``eth_fonts_of``); in those fonts the character is restored and marked as repaired.
    """
    import pypdfium2.raw as raw

    out = PageText()
    marks: list[Atom] = []
    textpage = page.get_textpage()
    try:
        height = page.get_height()
        count = raw.FPDFText_CountChars(textpage)
        skip_next = False
        for index in range(count):
            if skip_next:
                skip_next = False
                continue
            obj = raw.FPDFText_GetTextObject(textpage, index)
            if not obj:
                continue  # inserted by pdfium (spaces, line ends), not drawn by the page
            code = raw.FPDFText_GetUnicode(textpage, index)
            if 0xD800 <= code <= 0xDFFF and index + 1 < count:
                # pdfium returns UTF-16 code units per index: join a surrogate pair into one character.
                # In a line it has reordered as RTL, the low unit can come first.
                other = raw.FPDFText_GetUnicode(textpage, index + 1)
                high, low = (code, other) if code <= 0xDBFF else (other, code)
                if 0xD800 <= high <= 0xDBFF and 0xDC00 <= low <= 0xDFFF:
                    code = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
                    skip_next = True
            if 0xD800 <= code <= 0xDFFF:
                out.lone_surrogates += 1
                code = 0xFFFD
            ch = chr(code) if 0 <= code <= 0x10FFFF else "\ufffd"
            if unicodedata.category(ch) == "Cc" and ch not in "\t":
                out.controls += 1
                continue
            mode = raw.FPDFTextObj_GetTextRenderMode(obj)
            if mode == 3:
                out.invisible += 1
            left, bottom, right, top = textpage.get_charbox(index, loose=True)
            if skip_next:
                l2, b2, r2, t2 = textpage.get_charbox(index + 1, loose=True)
                left, bottom, right, top = min(left, l2), min(bottom, b2), max(right, r2), max(top, t2)
            size = raw.FPDFText_GetFontSize(textpage, index)
            if not ch.isspace() and not unicodedata.combining(ch):
                out.chars_total += 1
                name = _font_name(raw, textpage, index)
                out.fonts[name] = out.fonts.get(name, 0) + 1
            if not ch.isspace():
                # Invisible text (render mode 3) is masked too: it sits over the image that holds the same words.
                out.drawn_boxes.append((left, height - top, right, height - bottom))
            angle = raw.FPDFText_GetCharAngle(textpage, index)
            if angle > 0 and abs(math.sin(angle)) > ROTATED_SIN and not ch.isspace():
                # Rotated text has no horizontal baseline; line building would make one line per letter.
                out.rotated += 1
                continue
            repaired = None
            if ch == "ð" and eth_fonts and _font_name(raw, textpage, index) in eth_fonts:
                ch, repaired = "נ", repairs.LATIN1_ETH_NUN.rule_id
            atom = Atom(ch, left, height - top, right, height - bottom, size or 1.0, index, repaired)
            (marks if unicodedata.combining(ch) else out.atoms).append(atom)
    finally:
        textpage.close()
    _attach_marks(out.atoms, marks)
    return out


def _attach_marks(atoms: list[Atom], marks: list[Atom]) -> None:
    """Niqqud and other combining marks join the letter they sit on, in stream order.

    Marks are placed by position, not by stream order, because a visual-order stream draws
    a mark before its base letter.
    """
    letters = [a for a in atoms if not a.is_space]
    for mark in marks:
        best, best_key = None, None
        for letter in letters:
            inside = letter.x0 - 0.01 <= mark.cx <= letter.x1 + 0.01
            key = (not inside, abs(letter.cy - mark.cy), abs(letter.cx - mark.cx), abs(letter.index - mark.index))
            if best_key is None or key < best_key:
                best, best_key = letter, key
        if best is None:
            atoms.append(mark)
        else:
            best.text += mark.text


def eth_fonts_of(pdf) -> frozenset[str]:
    """Fonts whose ð is a broken נ: ð next to a Hebrew letter, and no נ anywhere in that font.

    Decided per font over the whole document, since one file can mix a broken font and a
    healthy one (the lead's review of D2c).
    """
    import pypdfium2.raw as raw

    eth: dict[str, int] = {}
    nun: dict[str, int] = {}
    for index in range(len(pdf)):
        page = pdf[index]
        textpage = page.get_textpage()
        try:
            count = raw.FPDFText_CountChars(textpage)
            codes = [raw.FPDFText_GetUnicode(textpage, i) for i in range(count)]
            for i, code in enumerate(codes):
                if code == 0x05E0:
                    name = _font_name(raw, textpage, i)
                    nun[name] = nun.get(name, 0) + 1
                elif code == 0x00F0:
                    near = codes[max(i - 1, 0) : i] + codes[i + 1 : i + 2]
                    if any(0x05D0 <= c <= 0x05EA for c in near):
                        name = _font_name(raw, textpage, i)
                        eth[name] = eth.get(name, 0) + 1
        finally:
            textpage.close()
            page.close()
    return frozenset(name for name in eth if not nun.get(name))


def read_t1(doc: Document, page_index: int, built: PageLines) -> int:
    """Read a T1 page's built lines into blocks. Returns the characters read."""
    read = 0
    for group in paragraphs(built):
        lines: list[Line] = []
        bullets: Counter = Counter()
        eths = sum(1 for line in group for atom in line.atoms if atom.repaired == repairs.LATIN1_ETH_NUN.rule_id)
        for line in group:
            text = line.text
            if doc.options.get("repairs", True):
                text, changes = repairs.symbol_font_bullet(text)
                if changes:
                    bullets.update(changes)
                    line.flags.append("bullet_symbol_mapped")
            lines.append(Line(text, line.bbox, line.direction))
        flags = sorted({flag for line in group for flag in line.flags})
        block_repairs = repairs.record(repairs.SYMBOL_FONT_BULLET, bullets)
        if eths:
            block_repairs += repairs.record(repairs.LATIN1_ETH_NUN, Counter({("U+00F0", "נ"): eths}))
        bbox = [
            min(line.bbox[0] for line in group),
            min(line.bbox[1] for line in group),
            max(line.bbox[2] for line in group),
            max(line.bbox[3] for line in group),
        ]
        block_text = "\n".join(line.text for line in lines)
        read += sum(1 for ch in block_text if not ch.isspace() and not unicodedata.combining(ch))
        doc.blocks.append(
            Block(
                kind="text",
                text=block_text,
                page=page_index,
                bbox=bbox,
                source="text_layer",
                tool=TOOL,
                direction=group[0].direction,
                lines=lines,
                repairs=block_repairs,
                flags=flags + text_flags(block_text),
            )
        )
    return read


def read_pdf(body: bytes, doc: Document) -> None:
    import pypdfium2 as pdfium
    import pypdfium2.version as pdfium_version

    doc.components["pypdfium2"] = str(pdfium_version.PYPDFIUM_INFO)
    doc.components["pdfium"] = str(pdfium_version.PDFIUM_INFO)
    try:
        pdf = pdfium.PdfDocument(body)
    except pdfium.PdfiumError as exc:
        reason = "pdf_encrypted" if "password" in str(exc).lower() else "pdf_unreadable"
        doc.unread.append(Unread(scope="document", reason=reason, detail=str(exc)[:200]))
        doc.flags.append("not_read")
        return
    try:
        doc.page_count = len(pdf)
        eth_fonts = eth_fonts_of(pdf) if doc.options.get("repairs", True) else frozenset()
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                _read_page(doc, index, page, eth_fonts)
            finally:
                page.close()
    finally:
        pdf.close()
    if doc.options.get("repairs", True):
        _repair_rtl_brackets(doc)


def _repair_rtl_brackets(doc: Document) -> None:
    """``rtl_bracket_order``, decided once per document on its RTL text-layer paragraphs."""
    blocks = [b for b in doc.blocks if b.source == "text_layer" and b.direction == "rtl" and b.lines]
    lines = [line.text for b in blocks for line in b.lines if line.direction == "rtl"]
    if not repairs.brackets_mirrored_in(lines):
        return
    for block in blocks:
        flat = block.text.replace("\n", " ")
        if repairs.bracket_order_valid(flat) and not repairs.bracket_order_valid(flat.translate(repairs.MIRROR)):
            # The paragraph's own evidence wins: balanced as read, broken when mirrored (another
            # source inside the document, such as photo credits). Left as read.
            block.flags.append("brackets_kept_valid_as_read")
            continue
        changes: Counter = Counter()
        for line in block.lines:
            if line.direction == "rtl":
                line.text, changed = repairs.rtl_bracket_order(line.text)
                changes.update(changed)
        if changes:
            block.text = "\n".join(line.text for line in block.lines)
            block.repairs += repairs.record(repairs.RTL_BRACKET_ORDER, changes)
            block.flags = [f for f in block.flags if f != "rtl_brackets_unverified"] + ["brackets_mirrored"]


def _report_side_losses(doc: Document, index: int, text: PageText, flags: list[str]) -> None:
    """Characters the page has but the route does not read are reported, never dropped quietly."""
    if text.rotated:
        flags.append("rotated_text_not_read")
        doc.unread.append(
            Unread(scope="page", reason="rotated_text_not_read", page=index, detail=f"{text.rotated} characters drawn at an angle")
        )
    if text.lone_surrogates:
        flags.append("lone_surrogates_replaced")


def _read_page(doc: Document, index: int, page, eth_fonts: frozenset[str] = frozenset()) -> None:
    width, height = page.get_size()
    text = page_text(page, eth_fonts)
    stats = object_stats(page)
    built = build_lines(text.atoms)
    chars = "".join(a.text for a in text.atoms)
    signals = text_signals(chars, built.lines, text.invisible, text.chars_total)
    signals.update(stream_signals(stats.stream_text))
    signals.update(
        {
            "images": stats.images,
            "image_coverage": round(stats.image_coverage, 4),
            "paths": stats.paths,
            "text_objects": stats.text_objects,
            "control_chars": text.controls,
            "width": round(width, 2),
            "height": round(height, 2),
            "fonts": [name for name, _ in sorted(text.fonts.items(), key=lambda kv: -kv[1])[:5]],
        }
    )
    page_class, flags = classify(signals)
    if page.get_rotation():
        flags.append("page_rotated")
    _report_side_losses(doc, index, text, flags)
    if page_class == "empty":
        doc.pages.append(
            Page(index, width, height, "empty", "none", signals, {"chars_total": 0, "chars_read": 0, "status": "nothing_to_read"}, flags)
        )
        return
    needs_ocr = page_class in OCR_CLASSES or (page_class == "T2" and "reversed_after_line_building" in flags)
    if needs_ocr:
        _ocr_page(doc, index, page, width, height, page_class, signals, text, flags)
        return
    if page_class not in READ_CLASSES:
        doc.pages.append(
            Page(
                index,
                width,
                height,
                page_class,
                "none",
                signals,
                {"chars_total": text.chars_total, "chars_read": 0, "status": "unread"},
                flags + ["route_unavailable"],
            )
        )
        doc.unread.append(Unread(scope="page", reason=f"class_{page_class}_no_route", page=index, detail=_NO_ROUTE[page_class]))
        return
    read = read_t1(doc, index, built)
    status = "read" if read >= text.chars_total else "partial"
    route = "text_layer"
    if page_class == "T6":
        # The text layer is read; each image region, which may hold text, goes to region OCR.
        ocr_regions = _ocr_regions(doc, index, page, stats.image_boxes, text.drawn_boxes)
        status = "partial" if ocr_regions < len(stats.image_boxes) else status
        if ocr_regions:
            route = "text_layer+ocr_regions"
    doc.pages.append(
        Page(
            index,
            width,
            height,
            page_class,
            route,
            signals,
            {
                "chars_total": text.chars_total,
                "chars_read": read,
                "status": status,
                "selection_safe": built.selection_safe,
            },
            flags + built.flags,
        )
    )


def _render(page) -> tuple[object, float]:
    """The page as a PIL image at RENDER_DPI, or lower when that would pass MAX_RENDER_PIXELS.

    Returns the image and the scale used (pixels per point). A huge or hostile page size
    cannot allocate billions of pixels.
    """
    width, height = page.get_size()
    scale = RENDER_DPI / 72
    if width * height * scale * scale > MAX_RENDER_PIXELS:
        scale = (MAX_RENDER_PIXELS / (width * height)) ** 0.5
    return page.render(scale=scale).to_pil(), scale


def _ocr_blocks(doc: Document, index: int, engine, result, render_scale: float, *, offset=(0.0, 0.0), flags=()) -> int:
    """OCR lines into blocks, one per Tesseract paragraph, with boxes in page points. Returns characters."""
    scale = 1 / render_scale
    dpi = round(render_scale * 72, 1)
    groups: list[list] = []
    for line in result.lines:
        if groups and groups[-1][-1].paragraph == line.paragraph:
            groups[-1].append(line)
        else:
            groups.append([line])
    read = 0
    for group in groups:
        lines = [
            Line(line.text, [offset[0] + v * scale if i % 2 == 0 else offset[1] + v * scale for i, v in enumerate(line.bbox)])
            for line in group
        ]
        text = "\n".join(line.text for line in lines)
        read += sum(1 for ch in text if not ch.isspace())
        doc.blocks.append(
            Block(
                kind="text",
                text=text,
                page=index,
                bbox=[min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines), max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)],
                source="ocr",
                tool=engine.version,
                direction="rtl",
                lines=lines,
                ocr={**block_ocr(engine, group), "dpi": dpi},
                flags=["ocr_unverified", *flags] + (["render_dpi_reduced"] if dpi < RENDER_DPI else []) + text_flags(text),
            )
        )
    return read


def _ocr_page(doc: Document, index: int, page, width, height, page_class, signals, text: PageText, flags) -> None:
    engine = engine_for(doc)
    if engine is None:
        reason = unavailable_reason(doc)
        doc.pages.append(
            Page(index, width, height, page_class, "none", signals,
                 {"chars_total": text.chars_total, "chars_read": 0, "status": "unread"}, flags + ["route_unavailable"])
        )
        doc.unread.append(Unread(scope="page", reason="ocr_route_unavailable", page=index, detail=f"class {page_class}; {reason}"))
        return
    image, scale = _render(page)
    if scale * 72 < RENDER_DPI:
        flags = flags + ["render_dpi_reduced"]
    signals = {**signals, "render_dpi": round(scale * 72, 1)}
    try:
        result = engine.recognize(image, dpi=round(scale * 72))
    except OcrUnavailable as exc:
        doc.pages.append(
            Page(index, width, height, page_class, "none", signals,
                 {"chars_total": text.chars_total, "chars_read": 0, "status": "unread"}, flags + ["ocr_failed"])
        )
        doc.unread.append(Unread(scope="page", reason="ocr_failed", page=index, detail=str(exc)))
        return
    read = _ocr_blocks(doc, index, engine, result, scale)
    if page_class == "T4":
        flags = flags + ["text_layer_ignored"]
    if not result.lines:
        doc.unread.append(Unread(scope="page", reason="ocr_found_no_text", page=index, detail=f"class {page_class}"))
    doc.pages.append(
        Page(index, width, height, page_class, "ocr", signals,
             {"chars_total": text.chars_total, "chars_read": 0, "chars_ocr": read,
              "status": "ocr" if result.lines else "unread"}, flags)
    )


def mask_layer_text(crop, origin: tuple[float, float], scale: float, boxes) -> tuple[object, int]:
    """Paint the text layer's character boxes white inside an image crop.

    ``origin`` is the crop's top-left corner in page points; ``boxes`` are character boxes in
    points. Returns the masked image and the number of characters masked.
    """
    from PIL import ImageDraw

    image = crop.convert("L")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    masked = 0
    for x0, y0, x1, y1 in boxes:
        left, top = (x0 - origin[0]) * scale - 1, (y0 - origin[1]) * scale - 1
        right, bottom = (x1 - origin[0]) * scale + 1, (y1 - origin[1]) * scale + 1
        if right <= 0 or bottom <= 0 or left >= width or top >= height:
            continue
        draw.rectangle([left, top, right, bottom], fill=255)
        masked += 1
    return image, masked


def has_ink(image) -> bool:
    """Whether a grey image holds anything darker than paper, beyond a few stray pixels."""
    histogram = image.histogram()
    dark = sum(histogram[:INK_LEVEL])
    return dark >= max(MIN_INK_PIXELS, MIN_INK_SHARE * image.size[0] * image.size[1])


def _ocr_regions(doc: Document, index: int, page, boxes: list[list[float]], drawn_boxes) -> int:
    """Region OCR for image regions of a page read from its text layer. Returns regions recognized.

    The layer's own characters are masked out of each region first, so text already read from
    the layer (labels over a diagram, text on a background image) is not read again by OCR.
    """
    if not boxes:
        return 0
    engine = engine_for(doc)
    if engine is None:
        reason = unavailable_reason(doc)
        for box in boxes:
            doc.unread.append(Unread(scope="page", reason="image_region_not_read", page=index, bbox=box, detail=reason))
        return 0
    image, scale = _render(page)
    done = 0
    for box in boxes:
        crop = [int(box[0] * scale), int(box[1] * scale), int(box[2] * scale + 0.999), int(box[3] * scale + 0.999)]
        if crop[2] - crop[0] < MIN_REGION_PX or crop[3] - crop[1] < MIN_REGION_PX:
            doc.unread.append(Unread(scope="page", reason="image_region_too_small", page=index, bbox=box))
            continue
        inside = [b for b in drawn_boxes if b[0] < box[2] and b[2] > box[0] and b[1] < box[3] and b[3] > box[1]]
        region, masked = mask_layer_text(image.crop(crop), (crop[0] / scale, crop[1] / scale), scale, inside)
        if not has_ink(region):
            reason = "image_region_text_in_layer" if masked else "image_region_blank"
            doc.unread.append(Unread(scope="page", reason=reason, page=index, bbox=box, detail=f"{masked} layer characters masked"))
            continue
        try:
            result = engine.recognize(region, dpi=round(scale * 72))
        except OcrUnavailable as exc:
            doc.unread.append(Unread(scope="page", reason="ocr_failed", page=index, bbox=box, detail=str(exc)))
            continue
        if not result.lines:
            doc.unread.append(Unread(scope="page", reason="image_region_no_text_found", page=index, bbox=box))
            continue
        confidence = result.mean_confidence
        if confidence is not None and confidence < REGION_MIN_CONFIDENCE:
            # Logos and drawings yield stray marks at low confidence; they are reported, not emitted.
            doc.unread.append(
                Unread(scope="page", reason="image_region_ocr_low_confidence", page=index, bbox=box, detail=f"mean confidence {confidence}")
            )
            continue
        # Region text follows the page's text-layer blocks; its place in the reading order is not resolved.
        flags = ("from_image_region", "reading_order_region_appended") + (("layer_text_masked",) if masked else ())
        _ocr_blocks(doc, index, engine, result, scale, offset=(crop[0] / scale, crop[1] / scale), flags=flags)
        done += 1
    return done


# Classes read through the text layer. T2 (a visual-order stream) is read by the same geometric
# line building, since D2a. T6 image regions go to region OCR.
READ_CLASSES = ("T1", "T2", "T6")
# Classes read by OCR: a foreign OCR layer (ignored) and image-only pages. T2 pages whose words
# are drawn reversed join them.
OCR_CLASSES = ("T4", "T5")
RENDER_DPI = 300
# Rendering is capped at this many pixels (heocr-engine uses 40 M); the dpi drops to fit.
MAX_RENDER_PIXELS = 40_000_000
# After masking the layer's characters, a region needs this much ink (grey level below INK_LEVEL)
# to be worth OCR: at least MIN_INK_PIXELS, and MIN_INK_SHARE of its area.
INK_LEVEL = 160
MIN_INK_PIXELS = 50
MIN_INK_SHARE = 0.0005
# Image regions smaller than this (in rendered pixels, either side) are icons, not text.
MIN_REGION_PX = 24
# Region OCR below this mean word confidence is stray marks from logos and drawings (dev split:
# below 50 the results were single characters and punctuation; from 80 up, real labels).
REGION_MIN_CONFIDENCE = 50.0

_NO_ROUTE = {
    "T3": "broken font mapping; repairs and the T3 route arrive in D2c and D2d",
}
