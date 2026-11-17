"""The PDF route: per-page text-layer reading through pypdfium2.

Characters and their boxes come from pdfium; line building and reading order are ours
(``heocr.pdf.lines``). pdfium's own text order is not used: it already reorders lines,
and that is the step this package owns.
"""

from __future__ import annotations

import ctypes
from collections import Counter
import math
import re
import unicodedata
from dataclasses import dataclass, field

from heocr import repairs
from heocr.contract import Block, Cell, Document, Line, Page, Unread, page_limit
from heocr.ocr import OcrUnavailable, block_ocr, engine_for, number_engine_for, unavailable_reason
from heocr.ocr.regions import (  # noqa: F401  (re-exported for callers of the PDF route)
    INK_LEVEL,
    MAX_RENDER_PIXELS,
    MIN_REGION_PX,
    REGION_MIN_CONFIDENCE,
    REGION_MIN_PAGE_SHARE,
    RENDER_DPI,
    has_ink,
    ink_outside,
    mask_layer_text,
    paper_level,
    short_for_ink,
)
from heocr.pdf.classify import FIGURE_IMAGE, classify, object_stats, stream_signals, text_signals
from heocr.pdf.lines import Atom, PageLines, build_lines, line_is_rtl, paragraphs
from heocr.pdf.columns import UNRESOLVED, Segment, arrange
from heocr.pdf.ocrtables import cell_crop, cell_has_ink, cell_psm, tables_in
from heocr.pdf.tables import find_tables, rulings, table_blocks
from heocr.textflags import is_private_use, text_flags

TOOL = "heocr.pdf"
# A document is RTL when Hebrew is at least this share of its letters: a Hebrew tender with English
# tables stays RTL, an English document with a Hebrew quotation does not.
RTL_DOCUMENT_SHARE = 0.25
# Fonts whose private-use codes are signs (bullets, boxes, arrows), not letters.
SYMBOL_FONT = re.compile(r"wingdings|webdings|symbol|dingbats", re.I)
# Characters whose baseline angle has a sine above this are rotated text.
ROTATED_SIN = 0.1


@dataclass
class PageText:
    """What the text layer of one page holds."""

    atoms: list[Atom] = field(default_factory=list)
    chars_total: int = 0  # non-space characters drawn by the page's own text objects
    outside: int = 0  # non-space characters drawn outside the page's visible box (not read)
    controls: int = 0
    invisible: int = 0
    lone_surrogates: int = 0
    rotated: int = 0  # characters drawn at an angle (vertical or rotated text), not read in D1
    # Private-use codes drawn only by symbol fonts (Wingdings, Symbol ...): signs, not broken letters.
    symbol_pua: set[str] = field(default_factory=set)
    other_pua: set[str] = field(default_factory=set)
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


def page_text(page, eth_fonts: frozenset[str] = frozenset(), font_maps: dict | None = None, all_fonts: bool = False) -> PageText:
    """Characters with loose boxes, in page coordinates with the origin at the top left.

    ``eth_fonts`` names the fonts whose ð is a broken נ (``latin1_eth_nun``, decided per font
    by ``eth_fonts_of``); in those fonts the character is restored and marked as repaired.
    ``font_maps`` (font -> {code: letter}) are the maps ``font_private_use_by_ocr`` learned for this
    document. Atoms carry their font name for private-use codes, or for every character with
    ``all_fonts``.
    """
    import pypdfium2.raw as raw

    from heocr.pdf.frame import frame_of

    out = PageText()
    marks: list[Atom] = []
    textpage = page.get_textpage()
    frame = frame_of(page)
    try:
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
            box = frame.box(left, bottom, right, top)
            if frame.outside(box):
                # Drawn outside the visible box: a viewer never shows it (a facing page left in the
                # layer, a slug). Counted and recorded as unread, never read into the page.
                if not ch.isspace():
                    out.outside += 1
                continue
            if not ch.isspace() and not unicodedata.combining(ch):
                out.chars_total += 1
                name = _font_name(raw, textpage, index)
                out.fonts[name] = out.fonts.get(name, 0) + 1
            if not ch.isspace():
                # Invisible text (render mode 3) is masked too: it sits over the image that holds the same words.
                out.drawn_boxes.append(box)
            angle = raw.FPDFText_GetCharAngle(textpage, index)
            if angle > 0 and abs(math.sin(angle)) > ROTATED_SIN and not ch.isspace():
                # Rotated text has no horizontal baseline; line building would make one line per letter.
                out.rotated += 1
                continue
            font = ""
            repaired = None
            if is_private_use(ch):
                font = _font_name(raw, textpage, index)
                symbolic = SYMBOL_FONT.search(font) is not None
                (out.symbol_pua if symbolic else out.other_pua).add(ch)
                if symbolic:
                    font = ""
                elif font_maps and ch in font_maps.get(font, {}):
                    ch, repaired = font_maps[font][ch], repairs.FONT_PRIVATE_USE_BY_OCR.rule_id
            elif all_fonts and not ch.isspace():
                font = _font_name(raw, textpage, index)
            if ch == "ð" and eth_fonts and _font_name(raw, textpage, index) in eth_fonts:
                ch, repaired = "נ", repairs.LATIN1_ETH_NUN.rule_id
            atom = Atom(ch, *box, size or 1.0, index, repaired, font)
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


def document_is_rtl(pdf, limit: int | None = None) -> bool | None:
    """The document's direction, from its script: RTL when Hebrew is at least ``RTL_DOCUMENT_SHARE`` of its letters.

    Tables take the document's direction, not their own (the volunteer's ``clean.py`` rule 8:
    an English table in a Hebrew document is still RTL). None when the text layer has no
    letters at all (a scan): the caller then goes by the OCR language.
    """
    import pypdfium2.raw as raw

    hebrew = latin = 0
    for index in range(len(pdf) if limit is None else min(len(pdf), limit)):
        page = pdf[index]
        textpage = page.get_textpage()
        try:
            for i in range(raw.FPDFText_CountChars(textpage)):
                code = raw.FPDFText_GetUnicode(textpage, i)
                if 0x05D0 <= code <= 0x05EA:
                    hebrew += 1
                elif 0x41 <= code <= 0x5A or 0x61 <= code <= 0x7A:
                    latin += 1
        finally:
            textpage.close()
            page.close()
    if hebrew + latin == 0:
        return None
    return hebrew > 0 and hebrew >= RTL_DOCUMENT_SHARE * (hebrew + latin)


RTL_LANGUAGES = frozenset({"heb", "yid", "ara", "fas", "urd", "syr"})


def _language_is_rtl(doc: Document) -> bool:
    """A scanned document's direction, from the OCR language it is read in (``heb`` by default)."""
    lang = (doc._ocr_settings or {}).get("lang") or "heb"
    return any(part in RTL_LANGUAGES for part in lang.split("+")[:1])


def eth_fonts_of(pdf, limit: int | None = None) -> frozenset[str]:
    """Fonts whose ð is a broken נ: ð next to a Hebrew letter, and no נ anywhere in that font.

    Decided per font over the whole document, since one file can mix a broken font and a
    healthy one (the lead's review of D2c).
    """
    import pypdfium2.raw as raw

    eth: dict[str, int] = {}
    nun: dict[str, int] = {}
    for index in range(len(pdf) if limit is None else min(len(pdf), limit)):
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
        # A learned map gives each letter at most one code per font, so the code is recoverable.
        by_ocr = Counter(
            (f"U+{ord(doc._font_codes[(atom.font, atom.text)]):04X}", atom.text)
            for line in group
            for atom in line.atoms
            if atom.repaired == repairs.FONT_PRIVATE_USE_BY_OCR.rule_id
        )
        for line in group:
            text = line.text
            if doc.options.get("repairs", True):
                text, changes = repairs.symbol_font_bullet(text)
                if changes:
                    bullets.update(changes)
                    line.flags.append("bullet_symbol_mapped")
            lines.append(Line(text, line.bbox, line.direction))
        flags = sorted({flag for line in group for flag in line.flags})
        block_repairs = _logged(bullets, eths, by_ocr)
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


def _logged(bullets: Counter, eths: int, by_ocr: Counter) -> list[dict]:
    """The repair log of a block read from the layer: bullets, ð per font, codes learned by OCR."""
    out = repairs.record(repairs.SYMBOL_FONT_BULLET, bullets)
    if eths:
        out += repairs.record(repairs.LATIN1_ETH_NUN, Counter({("U+00F0", "נ"): eths}))
    if by_ocr:
        out += repairs.record(repairs.FONT_PRIVATE_USE_BY_OCR, by_ocr)
    return out


def _log_cell_repairs(doc: Document, table: Block) -> None:
    """A table logs the repairs made in its cells, as a paragraph logs its own."""
    ledger = table._cell_repairs
    if ledger is None:
        return
    by_ocr = Counter({(f"U+{ord(doc._font_codes[(font, letter)]):04X}", letter): n for (font, letter), n in ledger.pua.items()})
    table.repairs += _logged(ledger.bullets, ledger.eth, by_ocr)
    table._cell_repairs = None


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
        pages = min(len(pdf), doc._max_pages)
        page_limit(doc, len(pdf))
        verdict = document_is_rtl(pdf, pages)
        doc._rtl = verdict if verdict is not None else _language_is_rtl(doc)
        eth_fonts = eth_fonts_of(pdf, pages) if doc.options.get("repairs", True) else frozenset()
        font_maps = _learn_font_maps(doc, pdf) if doc.options.get("repairs", True) else {}
        for index in range(pages):
            page = pdf[index]
            try:
                _read_page(doc, index, page, eth_fonts, font_maps)
            finally:
                page.close()
        if doc.options.get("check_numbers"):
            _check_numbers(doc, pdf, doc.options["check_numbers"])
    finally:
        pdf.close()
    if doc.options.get("repairs", True):
        _repair_rtl_brackets(doc)
    from heocr.pdf.furniture import mark_furniture
    from heocr.pdf.webprint import mark_site_menu

    mark_furniture(doc)
    mark_site_menu(doc)


def _learn_font_maps(doc: Document, pdf) -> dict[str, dict[str, str]]:
    """``font_private_use_by_ocr`` for one document; {} when there is nothing to learn or no OCR."""
    import pypdfium2.raw as raw

    candidates = []
    for index in range(min(len(pdf), doc._max_pages)):
        page = pdf[index]
        textpage = page.get_textpage()
        try:
            count = raw.FPDFText_CountChars(textpage)
            codes = [raw.FPDFText_GetUnicode(textpage, i) for i in range(count)]
            if any(
                0xE000 <= c <= 0xF8FF and any(0x05D0 <= n <= 0x05EA for n in codes[max(i - 1, 0) : i] + codes[i + 1 : i + 2])
                for i, c in enumerate(codes)
            ):
                candidates.append(index)
        finally:
            textpage.close()
            page.close()
    if not candidates:
        return {}
    engine = engine_for(doc)
    if engine is None:
        return {}
    from heocr.pdf.fontmap import WORD_PSM, learn

    font_letters: dict[str, Counter] = {}
    pages = []
    for index in range(min(len(pdf), doc._max_pages)):
        page = pdf[index]
        try:
            text = page_text(page, all_fonts=True)
        finally:
            page.close()
        for atom in text.atoms:
            if atom.font and "\u05d0" <= atom.text[:1] <= "\u05ea":
                font_letters.setdefault(atom.font, Counter())[atom.text[:1]] += 1
        if index in candidates:
            pages.append((index, text.atoms))

    def render(index: int):
        page = pdf[index]
        try:
            return _render(page)
        finally:
            page.close()

    def recognize(image):
        try:
            return engine.recognize(image, dpi=RENDER_DPI, psm=WORD_PSM).lines
        except OcrUnavailable:
            return []

    maps = learn(pages, render, recognize, font_letters)
    doc.font_maps = [fmap.record() for fmap in maps]
    for fmap in maps:
        for code, letter in fmap.letters.items():
            doc._font_codes[(fmap.font, letter)] = code
    return {fmap.font: fmap.letters for fmap in maps if fmap.letters}


def _check_numbers(doc: Document, pdf, scope: str = "suspect") -> None:
    """Numbers in text-layer lines and table cells against OCR of each line's own image (``heocr.pdf.numbers``).

    The second reading is in ``eng`` (``number_engine_for``): stock ``heb`` misreads digits too
    often to be a check. A table logs its cells' checks and repairs, as a paragraph logs its own.
    """
    from heocr.pdf.numbers import LINE_PSM, PAD_PT, compare, wanted

    # A target is (block, cell or None, line): a Line of a paragraph, or a cell's [text, bbox].
    candidates: dict[int, list[tuple]] = {}
    for block in doc.blocks:
        if block.source != "text_layer" or block.page is None:
            continue
        if block.lines:
            for line in block.lines:
                if wanted(line.text, "suspect" if scope == "tables" else scope):
                    candidates.setdefault(block.page, []).append((block, None, line))
        for row in block.cells or []:
            for cell in row:
                for line in cell._lines or []:
                    if wanted(line[0], "all" if scope == "tables" else scope):
                        candidates.setdefault(block.page, []).append((block, cell, line))
    if not candidates:
        return
    engine = number_engine_for(doc)
    if engine is None:
        doc.flags.append("numbers_not_checked")
        return
    repairs_on = doc.options.get("repairs", True)
    totals: dict[int, list[int]] = {}
    touched: dict[int, Block] = {}
    for index, items in sorted(candidates.items()):
        page = pdf[index]
        try:
            image, scale = _render(page)
        finally:
            page.close()
        for block, cell, line in items:
            text, box = (line[0], line[1]) if cell is not None else (line.text, line.bbox)
            crop = (
                max(0, int((box[0] - PAD_PT) * scale)),
                max(0, int((box[1] - PAD_PT) * scale)),
                min(image.size[0], int((box[2] + PAD_PT) * scale) + 1),
                min(image.size[1], int((box[3] + PAD_PT) * scale) + 1),
            )
            if crop[2] <= crop[0] or crop[3] <= crop[1]:
                continue  # a degenerate line box: nothing to read a second time
            try:
                result = engine.recognize(image.crop(crop), dpi=round(scale * 72), psm=LINE_PSM)
            except OcrUnavailable:
                continue
            checked, agreed, disagreed, confirmed = compare(text, " ".join(l.text for l in result.lines), result.mean_confidence)
            if not checked:
                continue
            if not repairs_on:
                # Not repaired, the reversed token stays wrong in the output: a disagreement.
                disagreed += len(confirmed)
                confirmed = []
            page_totals = totals.setdefault(index, [0, 0, 0])
            page_totals[0] += checked
            page_totals[1] += agreed
            page_totals[2] += disagreed
            checks = block.checks or {"numbers": 0, "agreed": 0, "disagreed": 0}
            checks["numbers"] += checked
            checks["agreed"] += agreed
            checks["disagreed"] += disagreed
            block.checks = checks
            touched[id(block)] = block
            if disagreed and cell is not None and "number_disagreement" not in cell.flags:
                cell.flags.append("number_disagreement")
            for token, reading in confirmed:
                text = text.replace(token, reading, 1)
                block.repairs.append({"rule": repairs.REVERSED_DIGIT_GROUPS.rule_id, "from": token, "to": reading, "count": 1})
                block.flags = [f for f in block.flags if f != "digit_groups_suspect"] + ["digit_groups_confirmed"]
                checks["agreed"] += 1
            if confirmed:
                if cell is not None:
                    line[0] = text
                    cell.text = "\n".join(l[0] for l in cell._lines)
                else:
                    line.text = text
    for block in touched.values():
        if block.lines:
            block.text = "\n".join(line.text for line in block.lines)
        if block.checks["disagreed"]:
            block.flags.append("number_disagreement")
        elif block.checks["agreed"] < block.checks["numbers"]:
            block.flags.append("numbers_unconfirmed")
    for page in doc.pages:
        if page.index in totals:
            checked, agreed, disagreed = totals[page.index]
            page.coverage.update({"numbers_checked": checked, "numbers_agreed": agreed, "numbers_disagreed": disagreed})


def _repair_rtl_brackets(doc: Document) -> None:
    """``rtl_bracket_order``, decided once per document on its RTL text-layer paragraphs."""
    blocks = [b for b in doc.blocks if b.source == "text_layer" and b.direction == "rtl" and b.lines]
    lines = [line.text for b in blocks for line in b.lines if line.direction == "rtl"]
    if not repairs.brackets_mirrored_in(lines):
        return
    # Table cells read from the layer follow the same document decision, cell by cell (D3).
    for table in (b for b in doc.blocks if b.kind == "table" and b.source == "text_layer" and b.direction == "rtl"):
        changes: Counter = Counter()
        for row in table.cells or []:
            for cell in row:
                flat = cell.text.replace("\n", " ")
                if not any(ch in flat for ch in "()[]{}"):
                    continue
                if repairs.bracket_order_valid(flat) and not repairs.bracket_order_valid(flat.translate(repairs.MIRROR)):
                    continue  # the cell's own evidence wins, as for a paragraph
                parts = []
                for part in cell.text.split("\n"):
                    if line_is_rtl(part, True):
                        part, changed = repairs.rtl_bracket_order(part)
                        changes.update(changed)
                    parts.append(part)
                cell.text = "\n".join(parts)
        if changes:
            table.repairs += repairs.record(repairs.RTL_BRACKET_ORDER, changes)
            table.flags.append("brackets_mirrored")
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
    if text.outside:
        flags.append("text_outside_page")
        doc.unread.append(
            Unread(scope="page", reason="text_outside_page", page=index, detail=f"{text.outside} characters drawn outside the page's visible box")
        )


def _read_page(doc: Document, index: int, page, eth_fonts: frozenset[str] = frozenset(), font_maps: dict | None = None) -> None:
    width, height = page.get_size()
    text = page_text(page, eth_fonts, font_maps)
    stats = object_stats(page)
    built = build_lines(text.atoms)
    chars = "".join(a.text for a in text.atoms)
    signals = text_signals(chars, built.lines, text.invisible, text.chars_total, text.symbol_pua - text.other_pua)
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
    # T3 reaches here only with codes no rule recovered: whole-page OCR, as the plan says.
    needs_ocr = page_class in OCR_CLASSES or page_class == "T3" or (page_class == "T2" and "reversed_after_line_building" in flags)
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
    # Ruled tables first: their characters go to cells, the rest to lines (D3b).
    tables = find_tables(*rulings(page)) if stats.paths else []
    table_flags = sorted({f for t in tables if not t.rows for f in t.flags if f == "table_grid_unresolved"})
    repairs_on = doc.options.get("repairs", True)
    # Two pages side by side are read as two, in the document's order (D4c).
    halves = _spread_halves(width, text.atoms, tables, doc._rtl) if "possible_spread" in flags else None
    if halves is not None:
        gutter, parts = halves
        flags = flags + ["spread_split"]
        signals = {**signals, "spread_gutter": [round(gutter[0], 2), round(gutter[1], 2)]}
    else:
        parts = [(None, text.atoms, tables)]
    in_tables = in_lines = line_ink = 0
    body_flags: list[str] = []
    selection_safe = True
    unruled = False
    all_tables: list[Block] = []
    for half, atoms, region_tables in parts:
        first = len(doc.blocks)
        region = _read_region(doc, index, atoms, region_tables, built if half is None else None, repairs_on)
        if half is not None:
            for block in doc.blocks[first:]:
                block.half = half
        in_tables += region["in_tables"]
        in_lines += region["in_lines"]
        line_ink += region["line_ink"]
        body_flags += [f for f in region["flags"] if f not in body_flags]
        selection_safe = selection_safe and region["selection_safe"]
        unruled = unruled or region["unruled"]
        all_tables += region["tables"]
        if region["rejected"] and "table_rejected_crosses_text" not in table_flags:
            table_flags.append("table_rejected_crosses_text")
    t_blocks = all_tables
    read = in_lines + in_tables
    # Silent loss check: every layer character ends up in a cell or in a line, never in neither.
    ink = sum(1 for a in text.atoms if not a.is_space)
    placed_ink = in_tables + line_ink
    if placed_ink != ink:
        flags = flags + ["coverage_mismatch"]
    # Image regions are accounted on every class read through the text layer (T1, T2, T6). When
    # images cover a T6 share of the page, whatever the class (a T2 page can too), they go to
    # region OCR; otherwise they are logos and icons, counted in coverage.
    ocr_wanted = stats.image_coverage >= FIGURE_IMAGE
    regions = _ocr_regions(doc, index, page, width, height, stats.image_boxes, text.drawn_boxes, ocr_wanted)
    status = "read" if read >= text.chars_total and not regions["image_regions_unread"] else "partial"
    route = "text_layer+ocr_regions" if regions["image_regions_ocr"] else "text_layer"
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
                "chars_in_tables": in_tables,
                "chars_in_lines": in_lines,
                **regions,
                "status": status,
                "selection_safe": selection_safe,
            },
            flags + body_flags + table_flags + (["ruled_tables"] if t_blocks else []) + (["unruled_tables"] if unruled else [])
            + (["image_region_tables"] if regions.get("image_region_tables") else []),
        )
    )


def _read_region(doc: Document, index: int, atoms: list[Atom], tables, built: PageLines | None, repairs_on: bool) -> dict:
    """Read one region of a text-layer page (the page, or one half of a spread): ruled tables,
    side-by-side rows, then lines. Blocks are appended in reading order, tables by their top edge."""
    t_blocks, body_atoms, in_tables, rejected, grids = table_blocks(index, atoms, tables, doc._rtl, repairs_on)
    if built is None or t_blocks:
        built = build_lines(body_atoms)
    # Side-by-side rows: rows of a ruled table, unruled tables, column flows (D3c).
    if t_blocks or built.has_columns:
        segments, t_blocks, moved = arrange(body_atoms, doc._rtl, index, list(zip(t_blocks, grids)), repairs_on)
        in_tables += moved
    else:
        segments = [Segment("lines", body_atoms)]
    first = len(doc.blocks)
    in_lines = 0
    line_ink = 0
    body_flags: list[str] = []
    selection_safe = True
    unruled = False
    for segment in segments:
        if segment.kind == "table":
            _log_cell_repairs(doc, segment.block)
            doc.blocks.append(segment.block)
            in_tables += segment.chars
            unruled = True
            # An unruled table is inferred from geometry alone: its cells' order is our reading,
            # not one the page shows, so the page cannot be called selection-safe.
            selection_safe = False
            continue
        parts = segment.bands if segment.kind in ("flow", "rows") else [segment.atoms]
        for part in parts:
            lines_built = built if len(segments) == 1 and segment.kind == "lines" and not t_blocks else build_lines(part)
            if segment.kind == "rows":
                lines_built.flags = [f for f in lines_built.flags if f != UNRESOLVED] + ["reading_order_row_major_unverified"]
            in_lines += read_t1(doc, index, lines_built)
            line_ink += sum(1 for a in part if not a.is_space)
            body_flags += [f for f in lines_built.flags if f not in body_flags]
            selection_safe = selection_safe and lines_built.selection_safe and segment.kind == "lines"
        if segment.kind == "flow" and "reading_order_column_major_unverified" not in body_flags:
            body_flags.append("reading_order_column_major_unverified")
    for table in t_blocks:
        _log_cell_repairs(doc, table)
    _place_tables(doc, first, t_blocks)
    return {
        "in_tables": in_tables,
        "in_lines": in_lines,
        "line_ink": line_ink,
        "flags": body_flags,
        "selection_safe": selection_safe,
        "unruled": unruled,
        "tables": t_blocks,
        "rejected": rejected,
    }


SPREAD_MIDDLE = 0.05  # the gutter reaches within this share of the width from the page's middle (the fold)
SPREAD_GUTTER_PT = 12.0  # and it is at least this wide, with no character crossing it


def _spread_halves(width: float, atoms: list[Atom], tables, rtl: bool):
    """(gutter, [(half, atoms, tables)] in reading order) for a page that is two pages side by
    side, or None without the evidence: a clean vertical gutter near the middle, text on both
    sides, and no table across it."""
    ink = sorted((a.x0, a.x1) for a in atoms if not a.is_space)
    if not ink:
        return None
    gaps = []
    reach = ink[0][1]
    for x0, x1 in ink[1:]:
        if x0 - reach >= SPREAD_GUTTER_PT:
            gaps.append((reach, x0))
        reach = max(reach, x1)
    middle = width / 2
    tolerance = SPREAD_MIDDLE * width
    # The gutter holds the fold: it reaches within the tolerance of the page's middle.
    near = [g for g in gaps if g[0] <= middle + tolerance and g[1] >= middle - tolerance]
    if not near:
        return None
    gutter = min(near, key=lambda g: max(0.0, g[0] - middle, middle - g[1]))
    cut = min(max(middle, gutter[0]), gutter[1])
    if any(t.bbox[0] < cut < t.bbox[2] for t in tables):
        return None
    left = [a for a in atoms if a.cx < cut]
    right = [a for a in atoms if a.cx >= cut]
    if not any(not a.is_space for a in left) or not any(not a.is_space for a in right):
        return None
    halves = {
        "left": (left, [t for t in tables if (t.bbox[0] + t.bbox[2]) / 2 < cut]),
        "right": (right, [t for t in tables if (t.bbox[0] + t.bbox[2]) / 2 >= cut]),
    }
    order = ("right", "left") if rtl else ("left", "right")
    return gutter, [(half, *halves[half]) for half in order]


def _place_tables(doc: Document, first: int, tables: list[Block]) -> None:
    """Insert a page's table blocks among its line blocks, by their top edge."""
    for table in sorted(tables, key=lambda t: t.bbox[1]):
        at = len(doc.blocks)
        for i in range(first, len(doc.blocks)):
            block = doc.blocks[i]
            if block.bbox is not None and block.bbox[1] > table.bbox[1]:
                at = i
                break
        doc.blocks.insert(at, table)


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


def _ocr_blocks(
    doc: Document, index: int, engine, result, render_scale: float, *, offset=(0.0, 0.0), flags=(), min_confidence: float | None = None
) -> int:
    """OCR lines into blocks, one per Tesseract paragraph, with boxes in page points. Returns characters.

    With ``min_confidence``, paragraphs below it are left out; the caller records them as unread.
    """
    scale = 1 / render_scale
    dpi = round(render_scale * 72, 1)
    groups: list[list] = []
    for line in result.lines:
        if groups and groups[-1][-1].paragraph == line.paragraph:
            groups[-1].append(line)
        else:
            groups.append([line])
    read = 0
    if min_confidence is not None:
        groups = [g for g in groups if (block_ocr(engine, g)["mean_confidence"] or 0) >= min_confidence]
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


def _ocr_page(doc: Document, index: int, page, width, height, page_class, signals, text: PageText, flags, rendered=None) -> None:
    """Read a page by OCR. ``rendered`` is (image, pixels per point) when the page is already an
    image (an image file, D4b); otherwise the PDF page is rendered."""
    engine = engine_for(doc)
    if engine is None:
        reason = unavailable_reason(doc)
        doc.pages.append(
            Page(index, width, height, page_class, "none", signals,
                 {"chars_total": text.chars_total, "chars_read": 0, "status": "unread"}, flags + ["route_unavailable"])
        )
        doc.unread.append(Unread(scope="page", reason="ocr_route_unavailable", page=index, detail=f"class {page_class}; {reason}"))
        return
    image, scale = rendered if rendered is not None else _render(page)
    if scale * 72 < RENDER_DPI and rendered is None:
        flags = flags + ["render_dpi_reduced"]
    signals = {**signals, "render_dpi": round(scale * 72, 1)}
    # Ruled tables in the image are read cell by cell; the rest of the page once, without them (D3e).
    grids = tables_in(image, scale)
    page_image = _without(image, scale, [t.bbox for t in grids]) if grids else image
    try:
        result = engine.recognize(page_image, dpi=round(scale * 72))
    except OcrUnavailable as exc:
        doc.pages.append(
            Page(index, width, height, page_class, "none", signals,
                 {"chars_total": text.chars_total, "chars_read": 0, "status": "unread"}, flags + ["ocr_failed"])
        )
        doc.unread.append(Unread(scope="page", reason="ocr_failed", page=index, detail=str(exc)))
        return
    first = len(doc.blocks)
    read = _ocr_blocks(doc, index, engine, result, scale)
    if grids:
        numbers = number_engine_for(doc) if doc.options.get("check_numbers", "suspect") else None
        t_blocks, in_cells = _ocr_tables(doc, index, engine, image, scale, grids, None if numbers is engine else numbers)
        _place_tables(doc, first, t_blocks)
        read += in_cells
        flags = flags + ["ocr_tables"]
        if any("table_cells_ink_unread" in t.flags for t in t_blocks):
            flags = flags + ["table_cells_ink_unread"]
    if page_class == "T4":
        flags = flags + ["text_layer_ignored"]
    elif page_class == "T3":
        flags = flags + ["text_layer_ignored_broken_mapping"]
    found = bool(result.lines) or read > 0  # text read in table cells counts (D3e)
    if not found:
        doc.unread.append(Unread(scope="page", reason="ocr_found_no_text", page=index, detail=f"class {page_class}"))
    short, outside_share = short_for_ink(page_image.convert("L"), result)
    if short and found:
        flags = flags + ["ocr_short_for_ink"]
        doc.unread.append(
            Unread(scope="page", reason="ocr_short_for_ink", page=index, detail=f"{outside_share:.0%} of the ink lies outside every OCR line")
        )
    doc.pages.append(
        Page(index, width, height, page_class, "ocr", signals,
             {"chars_total": text.chars_total, "chars_read": 0, "chars_ocr": read,
              "ink_outside_ocr_share": outside_share,
              "status": "ocr" if found and not short else "partial" if found else "unread"}, flags)
    )


def _without(image, scale: float, boxes) -> object:
    """A copy of the page image with the given boxes (points) painted in the paper's own grey
    (``paper_level``: not white, which a global threshold would split the page on)."""
    from PIL import ImageDraw

    # Only grey and RGB images take a grey fill as is: in RGBA the fill's fourth value would be the
    # alpha, and in "P" a palette index. Anything else is read as RGB first.
    out = image.copy() if image.mode in ("L", "RGB") else image.convert("RGB")
    level = paper_level(image)
    fill = level if out.mode == "L" else (level, level, level)
    draw = ImageDraw.Draw(out)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle([(x0 - 2) * scale, (y0 - 2) * scale, (x1 + 2) * scale, (y1 + 2) * scale], fill=fill)
    return out


def _ocr_tables(doc: Document, index: int, engine, image, scale: float, grids, numbers=None, offset=(0.0, 0.0)) -> tuple[list[Block], int]:
    """Table blocks for ruled tables found in a page image, each inked cell OCR'd as its own region.

    With ``numbers`` (the number model), a cell whose reading holds digits is read a second time.
    ``offset`` (points) places an image that is a crop of the page (an image region, T6).
    """
    ox, oy = offset
    dpi = round(scale * 72)
    blocks: list[Block] = []
    read = 0
    for grid in grids:
        rows: list[list[Cell]] = []
        lines_all = []
        unread_cells = 0
        psms: Counter = Counter()
        checks = {"numbers": 0, "agreed": 0, "disagreed": 0, "read_by_number_model": 0}
        for row in grid.rows:
            ordered = sorted(row, key=lambda cell: -cell["col"] if doc._rtl else cell["col"])
            out_row: list[Cell] = []
            inked = empty = 0
            for cell in ordered:
                if cell["placeholder"]:
                    out_row.append(Cell("", colspan=cell["colspan"], flags=["merged_above"]))
                    continue
                crop = cell_crop(image, cell["box"], scale)
                if not cell_has_ink(crop):
                    out_row.append(Cell("", colspan=cell["colspan"], rowspan=cell["rowspan"]))
                    continue
                inked += 1
                psm = cell_psm(crop)
                psms[psm] += 1
                try:
                    result = engine.recognize(crop, dpi=dpi, psm=psm)
                    text = "\n".join(line.text for line in result.lines if line.text.strip())
                    lines_all.extend(result.lines)
                    cell_flags = []
                except OcrUnavailable:
                    text, cell_flags = "", ["ocr_failed"]
                if not text.strip():
                    empty += 1
                    cell_flags.append("ocr_empty_with_ink")
                readings = None
                if text.strip() and numbers is not None and any(ch.isdigit() for ch in text):
                    first = {"model": engine.lang, "text": text, "confidence": result.mean_confidence}
                    text, readings = _second_number_reading(numbers, crop, dpi, psm, first, cell_flags, checks)
                read += sum(1 for ch in text if not ch.isspace())
                out_row.append(Cell(text, colspan=cell["colspan"], rowspan=cell["rowspan"], flags=cell_flags, readings=readings))
            unread_cells += empty
            if inked and empty == inked:
                boxes = [c["box"] for c in row if c["box"] is not None]
                bbox = [min(b[0] for b in boxes) + ox, min(b[1] for b in boxes) + oy, max(b[2] for b in boxes) + ox, max(b[3] for b in boxes) + oy]
                doc.unread.append(Unread(scope="page", reason="table_row_ink_unread", page=index, bbox=bbox, detail=f"{inked} inked cells, OCR read none"))
            rows.append(out_row)
        flags = ["ruled_table", "ocr_unverified", *grid.flags]
        if unread_cells:
            flags.append("table_cells_ink_unread")
        if checks["disagreed"]:
            flags.append("number_disagreement")
        if checks["read_by_number_model"]:
            flags.append("numbers_read_by_number_model")
        blocks.append(
            Block(
                kind="table",
                cells=rows,
                page=index,
                bbox=[grid.bbox[0] + ox, grid.bbox[1] + oy, grid.bbox[2] + ox, grid.bbox[3] + oy],
                source="ocr",
                tool=engine.version,
                direction="rtl" if doc._rtl else "ltr",
                ocr={**block_ocr(engine, lines_all), "psm": "per_cell", "cells_psm7": psms[7], "cells_psm6": psms[6], "dpi": round(scale * 72, 1)},
                checks=checks if checks["numbers"] or checks["read_by_number_model"] else None,
                flags=flags + (["render_dpi_reduced"] if scale * 72 < RENDER_DPI else []),
            )
        )
    return blocks, read


def _second_number_reading(numbers, crop, dpi: int, psm: int, first: dict, cell_flags: list[str], checks: dict) -> tuple[str, list | None]:
    """Amounts in an OCR'd cell read a second time, by the number model (``eng``).

    A **numeric cell** (no letters in either reading) takes the number model's reading when it is
    confident and differs: stock ``heb`` misreads digits too often (dev: a cell's "8,500,000" read
    as "00"). The cell is flagged ``number_read_by_number_model`` and keeps both readings in
    ``readings`` (model, text, confidence); the table stays ``source: "ocr"``, unverified. Both
    readings are OCR, so this chooses between readings and overwrites no layer text: a cell read
    from the layer is only ever checked. In any other cell the readings are only compared, and a
    disagreement is flagged, never resolved (D3d's asymmetry).
    """
    from heocr.pdf.numbers import MIN_CONFIDENCE, compare, significant

    text = first["text"]
    try:
        result = numbers.recognize(crop, dpi=dpi, psm=psm)
    except OcrUnavailable:
        return text, None
    second = "\n".join(line.text for line in result.lines if line.text.strip())
    confidence = result.mean_confidence
    if not any(ch.isalpha() for ch in text) and second and not any(ch.isalpha() for ch in second):
        if second.split() == text.split():
            checks["numbers"] += len(significant(text))
            checks["agreed"] += len(significant(text))
            return text, None
        if confidence is not None and confidence >= MIN_CONFIDENCE:
            # Both readings are OCR: this chooses between two readings, it overwrites no layer
            # text. Both are kept in the cell; the block stays source "ocr", unverified.
            checks["read_by_number_model"] += 1
            cell_flags.append("number_read_by_number_model")
            return second, [first, {"model": numbers.lang, "text": second, "confidence": confidence}]
    checked, agreed, disagreed, _ = compare(text, second, confidence)
    checks["numbers"] += checked
    checks["agreed"] += agreed
    checks["disagreed"] += disagreed
    if disagreed:
        cell_flags.append("number_disagreement")
    return text, None


def _ocr_regions(
    doc: Document, index: int, page, width: float, height: float, boxes: list[list[float]], drawn_boxes, ocr_wanted: bool = True
) -> dict:
    """Region OCR for the image regions of a page read from its text layer. Returns coverage counts.

    The layer's own characters are masked out of each region first, so text already read from
    the layer (labels over a diagram, text on a background image) is not read again by OCR.
    Regions under REGION_MIN_PAGE_SHARE of the page (logos, icons) are counted, not recognized.
    """
    # Every region lands in exactly one count: ocr + small + unread + no_ocr_needed == image_regions.
    counts = {
        "image_regions": len(boxes),
        "image_regions_ocr": 0,
        "image_regions_small": 0,
        "image_regions_unread": 0,
        "image_regions_no_ocr_needed": 0,
        "chars_ocr": 0,
    }
    if not boxes:
        return counts
    large = [b for b in boxes if ocr_wanted and (b[2] - b[0]) * (b[3] - b[1]) >= REGION_MIN_PAGE_SHARE * width * height]
    counts["image_regions_small"] = len(boxes) - len(large)
    if not large:
        return counts

    def unread(reason: str, box, detail: str | None = None) -> None:
        doc.unread.append(Unread(scope="page", reason=reason, page=index, bbox=box, detail=detail))
        counts["image_regions_unread"] += 1

    engine = engine_for(doc)
    if engine is None:
        reason = unavailable_reason(doc)
        for box in large:
            unread("image_region_not_read", box, reason)
        return counts
    image, scale = _render(page)
    for box in large:
        crop = [int(box[0] * scale), int(box[1] * scale), int(box[2] * scale + 0.999), int(box[3] * scale + 0.999)]
        if crop[2] - crop[0] < MIN_REGION_PX or crop[3] - crop[1] < MIN_REGION_PX:
            unread("image_region_too_small", box)
            continue
        inside = [b for b in drawn_boxes if b[0] < box[2] and b[2] > box[0] and b[1] < box[3] and b[3] > box[1]]
        region, masked = mask_layer_text(image.crop(crop), (crop[0] / scale, crop[1] / scale), scale, inside)
        if not has_ink(region):
            # Not an unread loss: the layer holds this region's text, or it holds none.
            reason = "image_region_text_in_layer" if masked else "image_region_blank"
            doc.unread.append(Unread(scope="page", reason=reason, page=index, bbox=box, detail=f"{masked} layer characters masked"))
            counts["image_regions_no_ocr_needed"] += 1
            continue
        offset = (crop[0] / scale, crop[1] / scale)
        tables, in_cells = _region_tables(doc, index, engine, region, scale, offset)
        if tables:
            # The region's tables are read cell by cell; the rest of the region once, without them.
            doc.blocks.extend(tables)
            counts["chars_ocr"] += in_cells
            counts["image_region_tables"] = counts.get("image_region_tables", 0) + len(tables)
            region = _without(region, scale, [[b.bbox[0] - offset[0], b.bbox[1] - offset[1], b.bbox[2] - offset[0], b.bbox[3] - offset[1]] for b in tables])
            if not has_ink(region):
                counts["image_regions_ocr"] += 1
                continue
        try:
            result = engine.recognize(region, dpi=round(scale * 72))
        except OcrUnavailable as exc:
            unread("ocr_failed", box, str(exc))
            continue
        if not result.lines:
            if tables:
                counts["image_regions_ocr"] += 1  # its tables were read; the rest held no text
                continue
            unread("image_region_no_text_found", box)
            continue
        confidence = result.mean_confidence
        if confidence is not None and confidence < REGION_MIN_CONFIDENCE:
            # Logos and drawings yield stray marks at low confidence; they are reported, not emitted.
            unread("image_region_ocr_low_confidence", box, f"mean confidence {confidence}")
            continue
        # Region text follows the page's text-layer blocks; its place in the reading order is not resolved.
        flags = ("from_image_region", "reading_order_region_appended") + (("layer_text_masked",) if masked else ())
        short, share = short_for_ink(region, result)
        if short:
            # In a region, ink beyond the text is often the drawing itself: flagged on the block, for review.
            flags += ("ocr_short_for_ink",)
        first = len(doc.blocks)
        counts["chars_ocr"] += _ocr_blocks(
            doc, index, engine, result, scale, offset=(crop[0] / scale, crop[1] / scale), flags=flags, min_confidence=REGION_MIN_CONFIDENCE
        )
        for block in doc.blocks[first:]:
            block.ocr["ink_outside_share"] = share
        paragraphs = len({line.paragraph for line in result.lines})
        if len(doc.blocks) - first < paragraphs:
            doc.unread.append(
                Unread(scope="page", reason="image_region_ocr_low_confidence", page=index, bbox=box,
                       detail=f"{paragraphs - (len(doc.blocks) - first)} of {paragraphs} paragraphs below confidence {REGION_MIN_CONFIDENCE:g}")
            )
        counts["image_regions_ocr"] += 1
    return counts


# A grid found in an image region is a table only when it holds records: charts, photos and
# diagrams have ruled lines too, but their "cells" hold bars, pictures, axis ticks or nothing.
REGION_TABLE_RECORD_ROWS = 2  # at least this many rows with REGION_TABLE_RECORD_CELLS cells of text
REGION_TABLE_RECORD_CELLS = 2
REGION_TABLE_MIN_ALNUM = 2  # a cell of text holds at least this many letters or digits (not "|", "1", ".")
REGION_TABLE_SOLID_LEVEL = 128  # pixels darker than this are solid ink (bars, photos), not light shading
REGION_TABLE_SOLID_SHARE = 0.5  # a cell more than this dark is solid
REGION_TABLE_MAX_SOLID_CELLS = 0.3  # a grid with more than this share of solid cells is a chart or a picture


def _solid_share(region, grid, scale: float) -> float:
    """The share of a grid's cells that are mostly solid ink: bars, photographs, filled shapes."""
    cells = [c for row in grid.rows for c in row if not c["placeholder"]]
    solid = 0
    for cell in cells:
        histogram = cell_crop(region, cell["box"], scale).convert("L").histogram()
        if sum(histogram[:REGION_TABLE_SOLID_LEVEL]) > REGION_TABLE_SOLID_SHARE * max(sum(histogram), 1):
            solid += 1
    return solid / max(len(cells), 1)


def _region_tables(doc: Document, index: int, engine, region, scale: float, offset) -> tuple[list[Block], int]:
    """Ruled tables inside an image region of a text-layer page (T6), read cell by cell (D3e).

    A grid is kept only as a table of **records**: at most ``REGION_TABLE_MAX_SOLID_CELLS`` of its
    cells solid (bars and photographs are not cells of text), at least ``REGION_TABLE_RECORD_ROWS``
    rows that each hold ``REGION_TABLE_RECORD_CELLS`` cells of text (``REGION_TABLE_MIN_ALNUM``
    letters or digits each: axis ticks and stray marks are not text), and a mean confidence of at
    least ``REGION_MIN_CONFIDENCE``. Otherwise the grid is dropped, with anything it recorded, and the
    region is read by region OCR as before.
    """
    grids = [g for g in tables_in(region, scale) if len(g.rows) >= REGION_TABLE_RECORD_ROWS and max(len(r) for r in g.rows) >= REGION_TABLE_RECORD_CELLS]
    grids = [g for g in grids if _solid_share(region, g, scale) <= REGION_TABLE_MAX_SOLID_CELLS]
    if not grids:
        return [], 0
    numbers = number_engine_for(doc) if doc.options.get("check_numbers", "suspect") else None
    kept: list[Block] = []
    total = 0
    for grid in grids:
        mark = len(doc.unread)
        blocks, read = _ocr_tables(doc, index, engine, region, scale, [grid], None if numbers is engine else numbers, offset=offset)
        block = blocks[0]
        records = sum(
            1 for row in block.cells
            if sum(1 for c in row if sum(ch.isalnum() for ch in c.text) >= REGION_TABLE_MIN_ALNUM) >= REGION_TABLE_RECORD_CELLS
        )
        confidence = (block.ocr or {}).get("mean_confidence")
        if records >= REGION_TABLE_RECORD_ROWS and (confidence is None or confidence >= REGION_MIN_CONFIDENCE):
            block.flags += ["from_image_region", "reading_order_region_appended"]
            kept.append(block)
            total += read
        else:
            del doc.unread[mark:]  # not a table: what it recorded goes with it
    return kept, total


# Classes read through the text layer. T2 (a visual-order stream) is read by the same geometric
# line building, since D2a. T6 image regions go to region OCR.
READ_CLASSES = ("T1", "T2", "T6")
# Classes read by OCR: a foreign OCR layer (ignored) and image-only pages. T2 pages whose words
# are drawn reversed join them.
OCR_CLASSES = ("T4", "T5")
_NO_ROUTE = {
    "T3": "broken font mapping; repairs and the T3 route arrive in D2c and D2d",
}
