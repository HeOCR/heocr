"""The PDF route: per-page text-layer reading through pypdfium2.

Characters and their boxes come from pdfium; line building and reading order are ours
(``heocr.pdf.lines``). pdfium's own text order is not used: it already reorders lines,
and that is the step this package owns.
"""

from __future__ import annotations

import ctypes
import math
import unicodedata
from dataclasses import dataclass, field

from heocr.contract import Block, Document, Line, Page, Unread
from heocr.pdf.lines import Atom, build_lines, paragraphs

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
    fonts: dict[str, int] = field(default_factory=dict)


def _font_name(raw, textpage, index: int) -> str:
    buffer = ctypes.create_string_buffer(256)
    flags = ctypes.c_int(0)
    length = raw.FPDFText_GetFontInfo(textpage, index, buffer, 256, ctypes.byref(flags))
    if length <= 0:
        return ""
    return buffer.value.decode("utf-8", "replace")


def page_text(page) -> PageText:
    """Characters with loose boxes, in page coordinates with the origin at the top left."""
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
            angle = raw.FPDFText_GetCharAngle(textpage, index)
            if angle > 0 and abs(math.sin(angle)) > ROTATED_SIN and not ch.isspace():
                # Rotated text has no horizontal baseline; line building would make one line per letter.
                out.rotated += 1
                continue
            atom = Atom(ch, left, height - top, right, height - bottom, size or 1.0, index)
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


def read_t1(doc: Document, page_index: int, text: PageText) -> tuple[int, list[str], bool]:
    """Read a T1 page into blocks. Returns (characters read, page flags, selection_safe)."""
    built = build_lines(text.atoms)
    read = 0
    for group in paragraphs(built):
        lines = [Line(line.text, line.bbox, line.direction) for line in group]
        flags = sorted({flag for line in group for flag in line.flags})
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
                flags=flags,
            )
        )
    return read, built.flags, built.selection_safe


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
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                _read_page(doc, index, page)
            finally:
                page.close()
    finally:
        pdf.close()


def _report_side_losses(doc: Document, index: int, text: PageText, flags: list[str]) -> None:
    """Characters the page has but the route does not read are reported, never dropped quietly."""
    if text.rotated:
        flags.append("rotated_text_not_read")
        doc.unread.append(
            Unread(scope="page", reason="rotated_text_not_read", page=index, detail=f"{text.rotated} characters drawn at an angle")
        )
    if text.lone_surrogates:
        flags.append("lone_surrogates_replaced")


def _read_page(doc: Document, index: int, page) -> None:
    width, height = page.get_size()
    text = page_text(page)
    flags: list[str] = []
    rotation = page.get_rotation()
    if rotation:
        flags.append("page_rotated")
    _report_side_losses(doc, index, text, flags)
    if text.chars_total == 0:
        doc.pages.append(
            Page(index, width, height, "empty", "none", coverage={"chars_total": 0, "chars_read": 0, "status": "nothing_to_read"}, flags=flags)
        )
        doc.unread.append(Unread(scope="page", reason="no_text_layer", page=index, detail="page classes arrive in D1f"))
        return
    read, order_flags, safe = read_t1(doc, index, text)
    flags += order_flags + ["class_not_measured"]
    doc.pages.append(
        Page(
            index,
            width,
            height,
            "T1",
            "text_layer",
            signals={"chars": text.chars_total},
            coverage={
                "chars_total": text.chars_total,
                "chars_read": read,
                "status": "read" if read >= text.chars_total else "partial",
                "selection_safe": safe,
            },
            flags=flags,
        )
    )
