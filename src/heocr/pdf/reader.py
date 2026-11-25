"""The PDF route: per-page text-layer reading through pypdfium2.

Characters and their boxes come from pdfium; line building and reading order are ours
(``heocr.pdf.lines``). pdfium's own text order is not used: it already reorders lines,
and that is the step this package owns.
"""

from __future__ import annotations

import copy
import ctypes
from bisect import bisect_right
from collections import Counter, defaultdict
from itertools import groupby, pairwise
import math
import re
import unicodedata
from dataclasses import dataclass, field

from heocr import repairs
from heocr.contract import Block, Cell, Document, Line, Page, Unread, page_limit, pages_left_unread, time_up
from heocr.ocr import OcrUnavailable, block_ocr, call_failures, engine_for, latin_reviewer_for, number_engine_for, routes, unavailable_reason
from heocr.ocr import layout as ocr_layout
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrWord
from heocr.ocr.regions import (  # noqa: F401  (re-exported for callers of the PDF route)
    INK_LEVEL,
    MAX_RENDER_PIXELS,
    MIN_INK_PIXELS,
    MIN_REGION_PX,
    REGION_MIN_CONFIDENCE,
    REGION_MIN_PAGE_SHARE,
    SHORT_INK_MIN_PX,
    SHORT_INK_SHARE,
    RENDER_DPI,
    dense_ink_outside,
    has_ink,
    ink_outside,
    mask_layer_text,
    paper_level,
    short_for_ink,
    skew_angle,
    text_rows,
    unrotate_box,
)
from heocr.pdf.classify import FIGURE_IMAGE, PAGE_IMAGE, classify, object_stats, stream_signals, text_signals
from heocr.pdf.frame import frame_of
from heocr.pdf.lines import Atom, PageLines, build_lines, line_is_rtl, logical_text, paragraphs
from heocr.pdf.columns import UNRESOLVED, Segment, arrange
from heocr.pdf.ocrlines import recover_photo_lines
from heocr.pdf.ocrforms import needs_form_probe, preserve_form_cell_lines
from heocr.pdf.ocrmargins import attach_margin_numbers
from heocr.pdf.ocrorder import order_across_disjoint_gaps
from heocr.pdf.ocrrows import TableRows, table_rows
from heocr.pdf.ocrfragments import join_camera_line_fragments, recover_narrow_lines, suppress_contained_fragments
from heocr.pdf.ocrpixel import recover_pixel_lines
from heocr.pdf.ocrsparse import recover_scan_lines
from heocr.pdf.ocrlatin import reread_latin_runs
from heocr.pdf.ocrtables import (
    CELL_TEXT_PAGE_EDGE, cell_crop, cell_has_faint_ink, cell_has_ink, cell_psm, faint_cell_image,
    image_rulings, isolated_cell_text, tables_in,
)
from heocr.pdf.perspective import rectify_grid
from heocr.pdf.proposed import PROPOSED, image_spans, merge_proposals
from heocr.pdf.tables import find_tables, rulings, table_blocks
from heocr.pdf.tabletidy import tidy
from heocr.textflags import is_private_use, text_flags

TOOL = "heocr.pdf"
# A document is RTL when Hebrew is at least this share of its letters: a Hebrew tender with English
# tables stays RTL, an English document with a Hebrew quotation does not.
RTL_DOCUMENT_SHARE = 0.25
# Fonts whose private-use codes are signs (bullets, boxes, arrows), not letters.
SYMBOL_FONT = re.compile(r"wingdings|webdings|symbol|dingbats", re.I)
# Characters whose baseline angle has a sine above this are rotated text.
ROTATED_SIN = 0.1
OCR_LAYER_NUMBER = re.compile(r"\d+(?:[.,:/]\d+)*%?")


@dataclass
class PageText:
    """What the text layer of one page holds."""

    atoms: list[Atom] = field(default_factory=list)
    chars_total: int = 0  # non-space characters drawn by the page's own text objects
    outside: int = 0  # non-space characters drawn outside the page's visible box (not read)
    controls: int = 0
    invisible: int = 0
    lone_surrogates: int = 0
    rotated: int = 0  # characters drawn at an angle (vertical or rotated text) and not built into lines
    rotated_atoms: list = field(default_factory=list)  # (Atom, angle in degrees in the page frame), for _rotated_blocks
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


def _mark_reflected_ocr_numbers(raw, objects) -> None:
    """Mark numeric objects whose GlyphLessFont text already runs in logical order."""
    for obj, atoms in objects:
        if len(atoms) < 2 or not OCR_LAYER_NUMBER.fullmatch("".join(atom.text for atom in atoms)):
            continue
        matrix = raw.FS_MATRIX()
        if not raw.FPDFPageObj_GetMatrix(obj, ctypes.byref(matrix)):
            continue
        if not (matrix.a < 0 < matrix.d and matrix.b == matrix.c == 0):
            continue
        if not all(a.index + 1 == b.index and a.cx > b.cx for a, b in pairwise(atoms)):
            continue
        marker = (atoms[0].index, len(atoms))
        for atom in atoms:
            atom.reflected_number = marker


def page_text(page, eth_fonts: frozenset[str] = frozenset(), font_maps: dict | None = None, all_fonts: bool = False, shifted: dict | None = None) -> PageText:
    """Characters with loose boxes, in page coordinates with the origin at the top left.

    ``eth_fonts`` names the fonts whose ð is a broken נ (``latin1_eth_nun``, decided per font
    by ``eth_fonts_of``); in those fonts the character is restored and marked as repaired.
    ``font_maps`` (font -> {code: letter}) are the maps ``font_private_use_by_ocr`` learned for this
    document. Atoms carry their font name for private-use codes, or for every character with
    ``all_fonts``.
    """
    import pypdfium2.raw as raw

    out = PageText()
    marks: list[Atom] = []
    glyphless_objects = {}
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
            name = ""
            if not ch.isspace() and not unicodedata.combining(ch):
                out.chars_total += 1
                name = _font_name(raw, textpage, index)
                out.fonts[name] = out.fonts.get(name, 0) + 1
            if not ch.isspace():
                # Invisible text (render mode 3) is masked too: it sits over the image that holds the same words.
                out.drawn_boxes.append(box)
            angle = raw.FPDFText_GetCharAngle(textpage, index)
            if angle >= 0 and abs(math.sin(frame.angle(angle))) > ROTATED_SIN and not ch.isspace():
                # Rotated text has no horizontal baseline; line building would make one line per letter.
                # It is kept apart and built in its own frame (_rotated_blocks).
                out.rotated += 1
                out.rotated_atoms.append((Atom(ch, *box, size or 1.0, index), round(math.degrees(frame.angle(angle))) % 360))
                continue
            font = ""
            repaired = None
            if shifted and (0xE0 <= code <= 0xFA or 0xF0E0 <= code <= 0xF0FA):
                name = _font_name(raw, textpage, index)
                if shifted.get(name) == ("pua" if code >= 0xF000 else "latin1"):
                    # cp1255_shifted_font: the font carries Hebrew as its cp1255 byte (decided per font).
                    ch, repaired, font = repairs.cp1255_letter(code), repairs.CP1255_SHIFTED_FONT.rule_id, name
            if repaired:
                pass
            elif is_private_use(ch):
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
            if mode == 3 and name.rsplit("+", 1)[-1] == "GlyphLessFont":
                key = ctypes.cast(obj, ctypes.c_void_p).value
                if key not in glyphless_objects:
                    glyphless_objects[key] = (obj, [])
                glyphless_objects[key][1].append(atom)
        _mark_reflected_ocr_numbers(raw, glyphless_objects.values())
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
        if MARK_SPLIT_STREAM_ORDER and not any(_on_letter(mark, letter) for letter in letters):
            # A layer that gives each mark its own slot beside the letters (Internet Archive's OCR layers
            # do: every character of a word gets an equal share of the word's box) places no mark on a
            # letter. Its stream is in logical order, a mark right after its base: the mark joins the
            # nearest letter before it in the stream, on its line, and that letter's box takes in the
            # mark's slot, so the slot does not read as a gap between words.
            before = [a for a in letters if a.index < mark.index and abs(a.cy - mark.cy) <= 0.5 * max(a.y1 - a.y0, 1.0)]
            if before:
                base = max(before, key=lambda a: a.index)
                base.text += mark.text
                base.x0, base.x1 = min(base.x0, mark.x0), max(base.x1, mark.x1)
                continue
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


MARK_SPLIT_STREAM_ORDER = True  # a mark on no letter joins the letter before it in the stream (see _attach_marks)


def _on_letter(mark: Atom, letter: Atom) -> bool:
    """Whether a mark sits on a letter: its centre inside the letter's box, on the same line."""
    return letter.x0 - 0.01 <= mark.cx <= letter.x1 + 0.01 and abs(letter.cy - mark.cy) <= 0.5 * max(letter.y1 - letter.y0, 1.0)


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


SHIFTED_MIN_WORDS = 10
SHIFTED_WORD_SHARE = 0.8
SHIFTED_FINAL_EDGE = 0.9
_FINAL_BYTES = (0xEA, 0xED, 0xEF, 0xF3, 0xF5)  # ך ם ן ף ץ


def shifted_fonts_of(pdf, limit: int | None = None) -> dict[str, str]:
    """Fonts that carry Hebrew as its cp1255 byte, as Latin-1 or at U+F000 + the byte
    (``cp1255_shifted_font``). Decided per font over the whole document; see the rule's evidence."""
    import pypdfium2.raw as raw

    words: dict[tuple[str, str], list[list[int]]] = {}  # (font, kind) -> words of shifted codes
    other_words: dict[str, int] = {}
    hebrew: set[str] = set()
    for index in range(len(pdf) if limit is None else min(len(pdf), limit)):
        page = pdf[index]
        textpage = page.get_textpage()
        try:
            count = raw.FPDFText_CountChars(textpage)
            current: list[int] = []
            current_font = ""
            for i in range(count + 1):
                code = raw.FPDFText_GetUnicode(textpage, i) if i < count else 0x20
                letter_like = 0xE0 <= code <= 0xFA or 0xF0E0 <= code <= 0xF0FA or (0x41 <= code <= 0x5A) or (0x61 <= code <= 0x7A) or (0x05D0 <= code <= 0x05EA)
                if letter_like:
                    name = _font_name(raw, textpage, i)
                    if 0x05D0 <= code <= 0x05EA:
                        hebrew.add(name)
                    if current and name != current_font:
                        _close_word(current, current_font, words, other_words)
                        current = []
                    current_font = name
                    current.append(code)
                elif current:
                    _close_word(current, current_font, words, other_words)
                    current = []
        finally:
            textpage.close()
            page.close()
    out: dict[str, str] = {}
    for (font, kind), found in words.items():
        if font in hebrew or len(found) < SHIFTED_MIN_WORDS:
            continue
        if len(found) < SHIFTED_WORD_SHARE * (len(found) + other_words.get(font, 0)):
            continue
        base = 0xF000 if kind == "pua" else 0
        finals = [(n, len(w)) for w in found if len(w) > 1 for n, code in enumerate(w) if code - base in _FINAL_BYTES]
        if len(finals) < 3:
            continue
        at_end = sum(1 for n, size in finals if n == size - 1) / len(finals)
        at_start = sum(1 for n, size in finals if n == 0) / len(finals)
        if max(at_end, at_start) >= SHIFTED_FINAL_EDGE:  # logical order, or visual order: one edge, consistently
            out[font] = kind
    return out


def _close_word(word: list[int], font: str, words: dict, other_words: dict) -> None:
    for kind, low, high in (("latin1", 0xE0, 0xFA), ("pua", 0xF0E0, 0xF0FA)):
        if all(low <= code <= high for code in word):
            words.setdefault((font, kind), []).append(list(word))
            return
    other_words[font] = other_words.get(font, 0) + 1


DESKEW_TEXT_MIN_DEG = 1.0  # a page with no ruled table is turned straight only from this skew (degrees)
RULE_PAD_PT = 2.0  # around a ruling found in a scan
# A kept OCR layer whose scan has this share of its ink (and SHORT_INK_MIN_PX), in dense areas,
# outside every layer line and ruling is partial. A correct layer leaves little or none, even over a
# scan skewed under an unskewed layer; a missing paragraph of a few lines on a full page leaves more.
# A line or two left out of the layer is below this and not detected.
LAYER_SCAN_SHORT_SHARE = 0.10
LAYER_SCAN_TILE_PT = 30.0  # uncovered ink counts only where it is dense, as text is (dense_ink_outside)
LAYER_SCAN_DENSITY = 0.07
try:  # Pillow >= 9.1
    from PIL.Image import Resampling as _Resampling

    _BICUBIC = _Resampling.BICUBIC
except ImportError:  # pragma: no cover
    from PIL.Image import BICUBIC as _BICUBIC

LAYER_SCAN_PAD = 0.5  # of the median layer line height: a scan's glyphs sit near, not on, their layer boxes


LAYER_CHECK_LINES = 6  # lines of a page's OCR layer read again, spread over the page
LAYER_CHECK_MIN_LETTERS = 8  # a line with fewer letters says too little
LAYER_CHECK_AGREEMENT = 0.5  # the median letter agreement under which the two readings differ
LAYER_MISSPELT_MIN = 0.15  # the layer's share of misspelt words above which it looks wrong on its own
LAYER_MISSPELT_MARGIN = 0.10  # ... and by how much it must exceed our reading's share
# When the two readings share barely a third of their letters, far below what a right layer shares with
# our reading, the layer's own misspelling needs less weight: a table page's layer is mostly short cells,
# numbers and dates, which a scrambled layer misspells less. The margin over our reading still holds.
LAYER_STRONG_DISAGREEMENT = 0.35
LAYER_MISSPELT_MIN_STRONG = 0.10
# ... and only when our reading is clearly credible: this confident, and with enough words of its own to be
# spell-checked. On real Internet Archive layers (Tesseract's, over pointed and two-column pages) the line
# pairing can fail outright (agreement 0.0) at our confidence 57-74, with the layer no worse than ours.
LAYER_STRONG_MIN_CONFIDENCE = 80.0
MISSPELT_MIN_WORDS = 5  # fewer words say nothing about spelling

_FINALS = frozenset("ךםןףץ")
_HAS_FINAL = frozenset("כמנפצ")
_HEBREW_WORD = re.compile(r"^[א-ת]{2,}$")


def misspelt_share(text: str) -> float | None:
    """The share of Hebrew words spelt in a way Hebrew never is: a final form (ך ם ן ף ץ) inside a
    word, or a word ending in the plain form of a letter that has a final one (כ מ נ פ צ). Words with
    a geresh, gershayim or quote (abbreviations, foreign words) are left out. A layer that is a
    scramble of the page's letters breaks this rule in a large share of its words; real text, and
    Tesseract's reading of it, almost never do. None for fewer than ``MISSPELT_MIN_WORDS`` words."""
    words = []
    for token in text.split():
        token = token.strip(".,;:!?()[]{}-–—")
        if _HEBREW_WORD.match(token):
            words.append(token)
    if len(words) < MISSPELT_MIN_WORDS:
        return None
    odd = sum(1 for w in words if any(ch in _FINALS for ch in w[:-1]) or w[-1] in _HAS_FINAL)
    return odd / len(words)


@dataclass
class LayerCheck:
    """A second reading of a few lines of an invisible OCR layer (``_check_ocr_layer``)."""

    agreement: float  # the median letter agreement between the layer and our line OCR
    confidence: float | None  # our line OCR's mean word confidence
    layer_misspelt: float | None  # the layer's share of misspelt words (``misspelt_share``)
    ocr_misspelt: float | None  # the same, in our reading of the sampled lines
    pooled: bool = False  # short lines were pooled, as no line had LAYER_CHECK_MIN_LETTERS letters
    digit_agreement: float | None = None  # the digits' agreement over the sampled lines (``_digit_agreement``)
    digits: int = 0  # the layer's digits in the sampled lines
    digit_confidence: float | None = None  # our reading's mean confidence on its words with digits

    @property
    def disagrees(self) -> bool:
        return self.agreement < LAYER_CHECK_AGREEMENT

    @property
    def layer_is_wrong(self) -> bool:
        """Our reading is the credible one: it disagrees, it is confident, and the layer is misspelt
        on its own evidence, well beyond our reading. Disagreement alone says nothing of which side is
        wrong: on a hard scan a good foreign layer can beat our OCR."""
        if not self.disagrees or self.confidence is None or self.confidence < REGION_MIN_CONFIDENCE:
            return False
        if (LAYER_POOLED_REPLACE and self.pooled and self.layer_misspelt is None and self.ocr_misspelt is None
                and self.agreement < LAYER_STRONG_DISAGREEMENT and self.confidence >= LAYER_POOLED_MIN_CONFIDENCE):
            # A page of short cells (codes, numbers, one-word cells) gives neither side words enough to
            # spell-check. There, a very confident reading that shares under a third of the layer's letters
            # replaces it: a right layer shares nearly all of them.
            return True
        strong = (self.agreement < LAYER_STRONG_DISAGREEMENT and self.confidence >= LAYER_STRONG_MIN_CONFIDENCE
                  and self.ocr_misspelt is not None)
        floor = LAYER_MISSPELT_MIN_STRONG if strong else LAYER_MISSPELT_MIN
        if self.layer_misspelt is None or self.layer_misspelt < floor:
            return False
        return self.layer_misspelt >= (self.ocr_misspelt or 0.0) + LAYER_MISSPELT_MARGIN

    @property
    def digits_agree(self) -> bool:
        """Our confident reading shares the layer's digits: on a layer of amounts, dates and codes, with
        no words to spell-check and next to no letters, that is the evidence the layer is right."""
        return _digits_agree(self.digit_agreement, self.digits, self.digit_confidence)

    def signals(self) -> dict[str, float]:
        out = {"ocr_layer_agreement": round(self.agreement, 3)}
        if self.digit_agreement is not None:
            out["ocr_layer_digit_agreement"] = round(self.digit_agreement, 3)
            out["ocr_layer_digits"] = self.digits
        if self.digit_confidence is not None:
            out["ocr_layer_digit_confidence"] = round(self.digit_confidence, 1)
        if self.confidence is not None:
            out["ocr_layer_check_confidence"] = round(self.confidence, 1)
        if self.layer_misspelt is not None:
            out["ocr_layer_misspelt_share"] = round(self.layer_misspelt, 3)
        if self.ocr_misspelt is not None:
            out["ocr_check_misspelt_share"] = round(self.ocr_misspelt, 3)
        if self.pooled:
            out["ocr_layer_check_pooled"] = 1
        return out


def _check_ocr_layer(doc: Document, page, built: PageLines) -> LayerCheck | None:
    """A second reading of an invisible OCR layer.

    Up to ``LAYER_CHECK_LINES`` lines with at least ``LAYER_CHECK_MIN_LETTERS`` letters, spread over the
    page, are OCR'd one by one (``--psm 7``) and compared with the layer, letters only. The result
    also holds our reading's confidence and both sides' misspelt-word shares, from which
    ``LayerCheck.layer_is_wrong`` decides. When no line has that many letters, short lines are pooled
    (``_pooled_lines``). None when there is no OCR engine, or nothing to check.
    """
    import difflib

    engine = engine_for(doc)
    if engine is None:
        return None
    letters = lambda text: sum(ch.isalpha() for ch in text)
    groups = [[line] for line in built.lines if letters(line.text) >= LAYER_CHECK_MIN_LETTERS]
    pooled = False
    if not groups and LAYER_CHECK_POOLED:
        groups, pooled = _pooled_lines(built.lines, letters), True
    if not groups:
        return None
    step = max(1, len(groups) // LAYER_CHECK_LINES)
    sample = groups[::step][:LAYER_CHECK_LINES]
    image, scale = _render(page, frame_of(page))
    ratios, confidences, readings = [], [], []
    read_lines: dict[int, tuple[str, list]] = {}  # id(line) -> (our reading, its words)

    def reading(line):
        if id(line) not in read_lines:
            box = line.bbox
            crop = image.crop((max(0, int((box[0] - 2) * scale)), max(0, int((box[1] - 2) * scale)), int((box[2] + 2) * scale), int((box[3] + 2) * scale)))
            result = engine.recognize(crop, dpi=round(scale * 72), psm=7)
            read_lines[id(line)] = (" ".join(line_.text for line_ in result.lines), [w for line_ in result.lines for w in line_.words])
        return read_lines[id(line)]

    for group in sample:
        texts = []
        for line in group:
            try:
                text, words = reading(line)
            except OcrUnavailable:
                return None
            texts.append(text)
            confidences.extend(w.confidence for w in words)
        text = " ".join(texts)
        readings.append(text)
        read = "".join(ch for ch in text if ch.isalpha())
        layer = "".join(ch for line in group for ch in line.text if ch.isalpha())
        ratios.append(difflib.SequenceMatcher(None, layer, read, autojunk=False).ratio())
    ratios.sort()
    try:
        digit_agreement, digits, digit_confidence = _digit_agreement(doc, built, image, scale)
    except OcrUnavailable:
        digit_agreement, digits, digit_confidence = None, 0, None
    return LayerCheck(
        agreement=ratios[len(ratios) // 2],
        confidence=sum(confidences) / len(confidences) if confidences else None,
        layer_misspelt=misspelt_share(" ".join(line.text for line in built.lines)),
        ocr_misspelt=misspelt_share(" ".join(readings)),
        pooled=pooled,
        digit_agreement=digit_agreement,
        digits=digits,
        digit_confidence=digit_confidence,
    )


def _digits_agree(agreement: float | None, digits: int, confidence: float | None) -> bool:
    return (agreement is not None and digits >= LAYER_DIGIT_MIN and agreement >= LAYER_DIGIT_KEEP
            and confidence is not None and confidence >= LAYER_DIGIT_MIN_CONFIDENCE)


def _layer_digits(doc: Document, page, built: PageLines) -> tuple[float | None, int, float | None]:
    """``_digit_agreement`` on its own, for a layer with no letters to check (a page of bare numbers)."""
    if number_engine_for(doc) is None:
        return None, 0, None
    image, scale = _render(page, frame_of(page))
    try:
        return _digit_agreement(doc, built, image, scale)
    except OcrUnavailable:
        return None, 0, None


def _digit_agreement(doc: Document, built: PageLines, image, scale: float) -> tuple[float | None, int, float | None]:
    """The digits of up to ``LAYER_CHECK_LINES`` layer lines with at least ``LAYER_DIGIT_MIN_LINE`` digits,
    spread over the page, against a reading of the same lines in ``eng`` (``number_engine_for``: stock
    ``heb`` misreads isolated digits, "860" as "00"): the median per-line agreement of the lines' numbers,
    the layer's digit count, and that reading's mean confidence on its words with digits."""
    engine = number_engine_for(doc)
    digit_lines = [line for line in built.lines if sum(ch.isdigit() for ch in line.text) >= LAYER_DIGIT_MIN_LINE]
    if engine is None or not digit_lines:
        return None, 0, None
    step = max(1, len(digit_lines) // LAYER_CHECK_LINES)
    ratios, digits, confidences = [], 0, []
    for line in digit_lines[::step][:LAYER_CHECK_LINES]:
        box = line.bbox
        crop = image.crop((max(0, int((box[0] - 2) * scale)), max(0, int((box[1] - 2) * scale)), int((box[2] + 2) * scale), int((box[3] + 2) * scale)))
        result = engine.recognize(crop, dpi=round(scale * 72), psm=7)
        text = " ".join(line_.text for line_ in result.lines)
        # Numbers as a bag, not a string: the two readings may order a row's cells differently.
        layer, ours = Counter(re.findall(r"\d+", line.text)), Counter(re.findall(r"\d+", text))
        digits += sum(len(n) * c for n, c in layer.items())
        both = sum(len(n) * c for n, c in (layer & ours).items())
        total = sum(len(n) * c for n, c in (layer + ours).items())
        ratios.append(2 * both / total if total else 0.0)
        confidences.extend(w.confidence for line_ in result.lines for w in line_.words if any(ch.isdigit() for ch in w.text))
    ratios.sort()
    return ratios[len(ratios) // 2], digits, (sum(confidences) / len(confidences) if confidences else None)


# A page whose layer has no line of LAYER_CHECK_MIN_LETTERS letters (a table of one-word cells) was not
# checked at all, so a wrong layer there was kept by default. Its short lines are
# pooled instead: consecutive lines of at least LAYER_POOL_MIN_LETTERS letters, in reading order, until a
# group reaches LAYER_CHECK_MIN_LETTERS. Each line is still read on its own crop.
LAYER_CHECK_POOLED = True
LAYER_POOL_MIN_LETTERS = 2
# Off: evaluation's held-out check (a fresh seed base) found no page for it to act on, so its gain is unshown.
# Kept, switchable, for a held-out set with all-table pages to test it.
LAYER_POOLED_REPLACE = False  # a pooled page with no spelling evidence on either side may be replaced (LayerCheck.layer_is_wrong)
LAYER_POOLED_MIN_CONFIDENCE = 85.0
# Digits: a layer of amounts, dates and codes has no words to spell-check, and its letters can be few (Latin
# codes, which a Hebrew model reads poorly). Its digits are compared with our reading of the same lines.
# A layer with neither Hebrew nor Latin text (``classify``) is kept only when they agree; otherwise it is
# read again by OCR, as a foreign layer is.
LAYER_DIGIT_MIN_LINE = 3  # a line with fewer digits says too little
LAYER_DIGIT_MIN = 30  # digits over the sampled lines
LAYER_DIGIT_KEEP = 0.8
LAYER_DIGIT_MIN_CONFIDENCE = 70.0


def _pooled_lines(lines, letters) -> list[list]:
    groups, group = [], []
    for line in lines:
        if letters(line.text) < LAYER_POOL_MIN_LETTERS:
            continue
        group.append(line)
        if sum(letters(item.text) for item in group) >= LAYER_CHECK_MIN_LETTERS:
            groups.append(group)
            group = []
    return groups


def read_t1(doc: Document, page_index: int, built: PageLines) -> int:
    """Read a T1 page's built lines into blocks. Returns the characters read."""
    read = 0
    for group in paragraphs(built):
        lines: list[Line] = []
        bullets: Counter = Counter()
        eths = sum(1 for line in group for atom in line.atoms if atom.repaired == repairs.LATIN1_ETH_NUN.rule_id)
        shifted = Counter(
            (_shifted_code(doc, atom), atom.text) for line in group for atom in line.atoms if atom.repaired == repairs.CP1255_SHIFTED_FONT.rule_id
        )
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
            punctuation: dict[int, str] = {}
            if text == line.text and line.direction == "rtl" and any(a.pdf_punctuation for a in line.atoms):
                lead = bool(lines) and repairs.continues_latin(lines[-1].text)
                rebuilt, _, _ = logical_text(line.atoms, True, lead, punctuation=punctuation)
                if rebuilt != text:
                    punctuation.clear()
            lines.append(Line(text, line.bbox, line.direction, punctuation))
        flags = sorted({flag for line in group for flag in line.flags})
        block_repairs = _logged(bullets, eths, by_ocr, shifted)
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


def _logged(bullets: Counter, eths: int, by_ocr: Counter, shifted: Counter | None = None) -> list[dict]:
    """The repair log of a block read from the layer: bullets, ð per font, codes learned by OCR,
    and cp1255-shifted fonts."""
    out = repairs.record(repairs.SYMBOL_FONT_BULLET, bullets)
    if eths:
        out += repairs.record(repairs.LATIN1_ETH_NUN, Counter({("U+00F0", "נ"): eths}))
    if by_ocr:
        out += repairs.record(repairs.FONT_PRIVATE_USE_BY_OCR, by_ocr)
    if shifted:
        out += repairs.record(repairs.CP1255_SHIFTED_FONT, shifted)
    return out


def _shifted_code(doc: Document, atom) -> str:
    """The code a cp1255-shifted font drew for a restored letter."""
    return _shifted_code_of(doc, atom.font, atom.text)


def _shifted_code_of(doc: Document, font: str, letter: str) -> str:
    byte = letter.encode("cp1255")[0]
    return f"U+{(0xF000 + byte) if doc._shifted_fonts.get(font) == 'pua' else byte:04X}"


def _log_cell_repairs(doc: Document, table: Block) -> None:
    """A table logs the repairs made in its cells, as a paragraph logs its own."""
    ledger = table._cell_repairs
    if ledger is None:
        return
    by_ocr = Counter({(f"U+{ord(doc._font_codes[(font, letter)]):04X}", letter): n for (font, letter), n in ledger.pua.items()})
    shifted = Counter({(_shifted_code_of(doc, font, letter), letter): n for (font, letter), n in ledger.shifted.items()})
    table.repairs += _logged(ledger.bullets, ledger.eth, by_ocr, shifted)
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
        if doc._table_proposer is not None:
            doc._pdf_body = body
        pages = min(len(pdf), doc._max_pages)
        page_limit(doc, len(pdf))
        verdict = document_is_rtl(pdf, pages)
        doc._rtl = verdict if verdict is not None else _language_is_rtl(doc)
        eth_fonts = eth_fonts_of(pdf, pages) if doc.options.get("repairs", True) else frozenset()
        doc._shifted_fonts = shifted_fonts_of(pdf, pages) if doc.options.get("repairs", True) else {}
        if doc._shifted_fonts:
            doc._rtl = True  # its letters are Hebrew, though the layer shows none
            doc.flags.append("font_cp1255_shifted")
        font_maps = _learn_font_maps(doc, pdf) if doc.options.get("repairs", True) else {}
        for index in range(pages):
            if time_up(doc):
                pages_left_unread(doc, index, pages)
                break
            page = pdf[index]
            failed = call_failures(doc)
            try:
                _read_page(doc, index, page, eth_fonts, font_maps, doc._shifted_fonts)
            finally:
                page.close()
            failed = call_failures(doc) - failed
            if failed and doc.pages and doc.pages[-1].index == index:
                # A Tesseract call on this page failed or timed out (a second reading, a check, a cell):
                # whatever it would have read is missing, and the page says so.
                doc.pages[-1].flags.append("ocr_call_failed")
                doc.pages[-1].signals["ocr_calls_failed"] = failed
        if doc.options.get("check_numbers"):
            _check_numbers(doc, pdf, doc.options["check_numbers"])
    finally:
        pdf.close()
    if doc.options.get("repairs", True):
        _repair_rtl_brackets(doc)
        _repair_latin_wraps(doc)
        _repair_witnessed_punctuation(doc)
    from heocr.pdf.furniture import mark_furniture, mark_ocr_running_headers
    from heocr.pdf.webprint import mark_site_footer, mark_site_menu

    mark_furniture(doc)
    mark_ocr_running_headers(doc)
    mark_site_menu(doc)
    mark_site_footer(doc)


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
            return _render(page, frame_of(page))
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
        if time_up(doc):
            # The text is read, so nothing is unread: its numbers on this page and after are not checked.
            doc.flags += [f for f in ("numbers_not_checked", "time_limit_reached") if f not in doc.flags]
            break
        page = pdf[index]
        try:
            image, scale = _render(page, frame_of(page))
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
    """``rtl_bracket_order``, decided once per document on its RTL text-layer paragraphs.

    Mirroring is its own inverse, so a block or table it already changed is never changed again:
    a second pass would put every bracket back as the layer had it."""
    done = ("brackets_mirrored", "brackets_kept_valid_as_read")
    blocks = [b for b in doc.blocks if b.source == "text_layer" and b.direction == "rtl" and b.lines]
    lines = [line.text for b in blocks for line in b.lines if line.direction == "rtl"]
    if not repairs.brackets_mirrored_in(lines):
        return
    # Table cells read from the layer follow the same document decision, cell by cell (D3).
    for table in (b for b in doc.blocks if b.kind == "table" and b.source == "text_layer" and b.direction == "rtl" and "brackets_mirrored" not in b.flags):
        changes: Counter = Counter()
        for row in table.cells or []:
            for cell in row:
                flat = cell.text.replace("\n", " ")
                if not any(ch in flat for ch in "()[]{}"):
                    continue
                if repairs.bracket_order_valid(flat) and not repairs.bracket_order_valid(repairs.mirror_brackets(flat)):
                    continue  # the cell's own evidence wins, as for a paragraph
                parts = []
                for part in cell.text.split("\n"):
                    if line_is_rtl(part, True):
                        lead = bool(parts) and repairs.continues_latin(parts[-1])
                        part, changed = repairs.rtl_bracket_order(part, lead)
                        changes.update(changed)
                    parts.append(part)
                cell.text = "\n".join(parts)
        if changes:
            table.repairs += repairs.record(repairs.RTL_BRACKET_ORDER, changes)
            table.flags.append("brackets_mirrored")
    for block in blocks:
        if any(flag in block.flags for flag in done):
            continue
        flat = block.text.replace("\n", " ")
        mirrored = repairs.mirror_brackets(flat)
        labels_say_mirror = repairs.bracket_labels(mirrored) > repairs.bracket_labels(flat)
        if repairs.bracket_order_valid(flat) and not repairs.bracket_order_valid(mirrored) and not labels_say_mirror:
            # The paragraph's own evidence wins: balanced as read, broken when mirrored (another
            # source inside the document, such as photo credits). Left as read. Unless its section
            # labels read right only mirrored ("(ג)" read as ")ג("): then the balance as read is a
            # false pair, a parenthesis closed from the paragraph before with one opened into the next.
            block.flags.append("brackets_kept_valid_as_read")
            continue
        changes: Counter = Counter()
        previous = ""
        for line in block.lines:
            if line.direction == "rtl":
                line.text, changed = repairs.rtl_bracket_order(line.text, repairs.continues_latin(previous))
                changes.update(changed)
            previous = line.text
        if changes:
            block.text = "\n".join(line.text for line in block.lines)
            block.repairs += repairs.record(repairs.RTL_BRACKET_ORDER, changes)
            block.flags = [f for f in block.flags if f != "rtl_brackets_unverified"] + ["brackets_mirrored"]


def _repair_witnessed_punctuation(doc: Document) -> None:
    """Apply exact glyph observations after the legacy text-layer passes."""
    for block in doc.blocks:
        changes: Counter = Counter()
        for line in block.lines or ():
            if not line._pdf_punctuation:
                continue
            line.text, changed = repairs.pdf_bidi_punctuation(line.text, line._pdf_punctuation)
            changes.update(changed)
        if changes:
            block.text = "\n".join(line.text for line in block.lines)
            block.repairs += repairs.record(repairs.PDF_BIDI_PUNCTUATION, changes)


def _repair_latin_wraps(doc: Document) -> None:
    """``latin_wrap_brackets`` in the RTL text-layer paragraphs and table cells, after the document's
    bracket order is decided."""
    for block in doc.blocks:
        if block.source != "text_layer" or block.direction != "rtl":
            continue
        changes: Counter = Counter()
        if block.kind == "table":
            for row in block.cells or []:
                for cell in row:
                    if "\n" in cell.text:
                        cell.text, changed = repairs.latin_wrap_brackets(cell.text)
                        changes.update(changed)
        elif block.lines and len(block.lines) > 1:
            text, changed = repairs.latin_wrap_brackets("\n".join(line.text for line in block.lines))
            if changed:
                for line, part in zip(block.lines, text.split("\n")):
                    line.text = part
                block.text = text
                changes.update(changed)
        if changes:
            block.repairs += repairs.record(repairs.LATIN_WRAP_BRACKETS, changes)


ROTATED_SNAP_DEG = 10  # rotated characters within this of 90 or 270 degrees are read as vertical lines


def _turn(box, angle: int):
    """A page box (top-left origin) in the upright frame of text drawn at ``angle`` (pdfium's,
    clockwise: 90 or 270), and back with ``-angle``. A PDF advances glyphs along text space x, the
    upright line's left to right: at 90 degrees that runs down the page and the next line lies to
    the left; at 270 it runs up the page and the next line lies to the right."""
    x0, y0, x1, y1 = box
    if angle == 90:
        return (y0, -x1, y1, -x0)
    if angle == -90:
        return (-y1, x0, -y0, x1)
    if angle == 270:
        return (-y1, x0, -y0, x1)
    return (y0, -x1, y1, -x0)  # -270: back from the 270 frame


def _rotated_blocks(doc: Document, index: int, rotated) -> tuple[list[Block], int]:
    """Rotated layer text read as its own lines. Characters drawn at about 90 or 270 degrees (a
    vertical margin note, a stamp's caption) are turned upright, built into lines as any other
    text, and given back as blocks with their boxes on the page, flagged ``rotated_text``. Other
    angles are left to ``rotated_text_not_read``. Returns the blocks and the characters placed."""
    blocks: list[Block] = []
    placed = 0
    for target in (90, 270):
        atoms = []
        for atom, angle in rotated:
            if min(abs(angle - target), 360 - abs(angle - target)) <= ROTATED_SNAP_DEG:
                x0, y0, x1, y1 = _turn((atom.x0, atom.y0, atom.x1, atom.y1), target)
                atoms.append(Atom(atom.text, x0, y0, x1, y1, atom.size, atom.index))
        if not atoms:
            continue
        built = build_lines(atoms, rtl=doc._rtl)
        for group in paragraphs(built):
            lines = [Line(line.text, list(_turn(line.bbox, -target)), line.direction) for line in group]
            bbox = [min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines), max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)]
            text = "\n".join(line.text for line in lines)
            placed += sum(1 for line in group for a in line.atoms if not a.is_space)
            blocks.append(Block(kind="text", text=text, page=index, bbox=bbox, source="text_layer", tool=TOOL,
                                direction=group[0].direction, lines=lines,
                                flags=["rotated_text", f"rotated_{target}", "reading_order_rotated_appended"] + text_flags(text)))
    return blocks, placed


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


def _read_page(doc: Document, index: int, page, eth_fonts: frozenset[str] = frozenset(), font_maps: dict | None = None, shifted: dict | None = None) -> None:
    width, height = page.get_size()
    text = page_text(page, eth_fonts, font_maps, shifted=shifted)
    stats = object_stats(page)
    built = build_lines(text.atoms)
    chars = "".join(a.text for a in text.atoms)
    signals = text_signals(chars, built.lines, text.invisible, text.chars_total, text.symbol_pua - text.other_pua)
    signals.update(stream_signals(stats.stream_text))
    if stats.painted_whitespace:
        signals["painted_whitespace"] = True
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
    shifted_chars = sum(1 for a in text.atoms if a.repaired == repairs.CP1255_SHIFTED_FONT.rule_id)
    if shifted_chars:
        signals["cp1255_shifted_chars"] = shifted_chars
    page_class, flags = classify(signals)
    if shifted_chars:
        # The layer was a cp1255-shifted font (a T3 layer by its state), fully restored: read as T1.
        flags.append("font_cp1255_shifted")
    forced = bool(doc.options.get("force_ocr")) and page_class != "empty"
    if forced and "ocr_layer_numbers_and_codes" in flags:
        # The layer is not checked, so the page keeps the class it had before its digits could decide.
        page_class, flags = "T4", [f for f in flags if f not in ("text_layer_is_ocr", "ocr_layer_numbers_and_codes")] + ["foreign_ocr_layer"]
    if forced:
        flags = flags + ["ocr_forced"]  # the user asked for OCR: the layer is neither checked nor read
    if "text_layer_is_ocr" in flags and page_class in ("T1", "T2") and not forced:
        # An invisible layer over a full-page image is someone's OCR. A second reading of a few of
        # its lines tells whether the two agree; when they do not, the layer is replaced only when
        # our reading is the credible one (``LayerCheck.layer_is_wrong``). Otherwise it is kept.
        check = _check_ocr_layer(doc, page, built)
        # A layer with neither Hebrew nor Latin text, such as a table of amounts, dates and codes, used to be
        # read again as foreign. It is kept only when its digits agree with our reading; otherwise it is
        # foreign, with the same class and flags as before.
        if check is not None:
            signals.update(check.signals())
        if "ocr_layer_numbers_and_codes" in flags:
            if check is not None:
                agree = check.digits_agree
            else:
                digit_agreement, digits, digit_confidence = _layer_digits(doc, page, built)
                agree = _digits_agree(digit_agreement, digits, digit_confidence)
                if digit_agreement is not None:
                    signals.update({"ocr_layer_digit_agreement": round(digit_agreement, 3), "ocr_layer_digits": digits})
                if digit_confidence is not None:
                    signals["ocr_layer_digit_confidence"] = round(digit_confidence, 1)
            if agree:
                flags.append("ocr_layer_kept_by_digits")
            else:
                dropped = ("text_layer_is_ocr", "ocr_layer_numbers_and_codes")
                page_class, flags = "T4", [f for f in flags if f not in dropped] + ["foreign_ocr_layer"]
        elif check is None:
            flags.append("ocr_layer_unchecked")
        else:
            if check.layer_is_wrong:
                page_class, flags = "T4", flags + ["foreign_ocr_layer", "ocr_layer_disagrees"]
            elif check.disagrees:
                flags.append("ocr_layer_disagrees_kept")
    if page.get_rotation():
        flags.append("page_rotated")
    rotated_blocks: list[Block] = []
    rotated_read = 0
    layer_read = not forced and page_class not in OCR_CLASSES and page_class not in ("T3", "empty") and not (page_class == "T2" and "reversed_after_line_building" in flags)
    if layer_read and text.rotated_atoms:
        rotated_blocks, placed = _rotated_blocks(doc, index, text.rotated_atoms)
        text.rotated -= placed
        rotated_read = sum(1 for b in rotated_blocks for ch in b.text if not ch.isspace() and not unicodedata.combining(ch))
    _report_side_losses(doc, index, text, flags)
    if page_class == "empty":
        doc.pages.append(
            Page(index, width, height, "empty", "none", signals, {"chars_total": 0, "chars_read": 0, "status": "nothing_to_read"}, flags)
        )
        return
    # T3 reaches here only with codes no rule recovered: whole-page OCR, as the plan says.
    needs_ocr = forced or page_class in OCR_CLASSES or page_class == "T3" or (page_class == "T2" and "reversed_after_line_building" in flags)
    if needs_ocr:
        _ocr_page(doc, index, page, width, height, page_class, signals, text, flags)
        return
    # From here the page is read from its text layer, in the frame its positions are in (the page
    # as displayed, or the user frame when the layer is sideways on screen): the page's size is
    # that frame's, so its blocks' boxes and its size agree.
    layer = frame_of(page)
    width, height = layer.width, layer.height
    if layer.sideways:
        flags = flags + ["text_sideways_on_screen"]
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
    if doc._table_proposer is not None:
        # Learned proposals (D3g) for tables without rulings: native grids keep first claim, before arrange.
        ink = [(a.x0, a.y0, a.x1, a.y1) for a in text.atoms if not a.is_space]
        tables, proposal_flags = _table_proposals(doc, index, tables, width, height, layer.sideways, ink)
        table_flags = table_flags + proposal_flags
    repairs_on = doc.options.get("repairs", True)
    if repairs_on and text.invisible == 0:
        candidates = [atom for line in built.lines if line.direction == "rtl"
                      for atom in line.atoms if atom.text in repairs.PDF_PUNCTUATION_PAIRS]
        if candidates and not time_up(doc):
            from heocr.pdf.punctuation import observe

            observe(page, candidates)
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
    if rotated_blocks:
        # Rotated text (a vertical margin note) follows the page's body, read in its own frame.
        doc.blocks.extend(rotated_blocks)
        read += rotated_read
        flags = flags + ["rotated_text_read"]
    # Silent loss check: every layer character ends up in a cell or in a line, never in neither.
    ink = sum(1 for a in text.atoms if not a.is_space)
    placed_ink = in_tables + line_ink
    if placed_ink != ink:
        flags = flags + ["coverage_mismatch"]
    # Image regions are accounted on every class read through the text layer (T1, T2, T6). When
    # images cover a T6 share of the page, whatever the class (a T2 page can too), they go to
    # region OCR; otherwise they are logos and icons, counted in coverage.
    ocr_wanted = stats.image_coverage >= FIGURE_IMAGE
    regions = _ocr_regions(doc, index, page, width, height, stats.image_boxes, text.drawn_boxes, ocr_wanted,
                           layer_is_ocr="text_layer_is_ocr" in flags, digests=stats.image_digests)
    if regions.pop("layer_scan_short_for_ink", 0):
        flags = flags + ["ocr_short_for_ink"]
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


def _table_proposals(doc: Document, index: int, tables, width: float, height: float, sideways: bool, ink=None):
    """Native ruled grids plus non-overlapping TableFormer proposals, and the page flags that say so."""
    if sideways:
        return tables, ["table_proposals_skipped_sideways"]
    try:
        proposals = doc._table_proposer.page_tables(doc._pdf_body, index, width, height)
    except Exception as exc:  # the optional proposer failing never stops the reading
        if "table_proposer_failed" not in doc.flags:
            doc.flags.append("table_proposer_failed")
        doc.unread.append(Unread(scope="page", reason="table_proposer_failed", page=index, detail=str(exc)[:200]))
        return tables, ["table_proposer_failed"]
    merged = merge_proposals(tables, proposals, ink)
    return merged, (["tableformer_tables"] if len(merged) > len(tables) else [])


def _ocr_table_proposals(doc: Document, index: int, image, scale: float, grids, camera_jpeg: bool):
    """Ruled grids found in a page image plus non-overlapping TableFormer proposals, and the page flags.

    Docling sees the page image exactly as heocr reads it (after any straightening), wrapped in a one-page PDF of
    the same size in points, so the proposals' boxes meet the image. A camera photo is skipped: its warped rows do
    not fit an upright grid, and the phone geometry path reads it.
    """
    settings = doc._ocr_settings or {}
    if camera_jpeg or settings.get("phone_geometry"):
        return grids, ["table_proposals_skipped_camera"]
    if time_up(doc):
        return grids, []
    import io

    gray = image.convert("L")
    body = io.BytesIO()
    gray.save(body, "PDF", resolution=scale * 72)
    try:
        proposals = doc._table_proposer.page_tables(body.getvalue(), 0, image.width / scale, image.height / scale)
    except Exception as exc:  # the optional proposer failing never stops the reading
        if "table_proposer_failed" not in doc.flags:
            doc.flags.append("table_proposer_failed")
        doc.unread.append(Unread(scope="page", reason="table_proposer_failed", page=index, detail=str(exc)[:200]))
        return grids, ["table_proposer_failed"]
    merged = merge_proposals(grids, proposals, spans=lambda box: image_spans(gray, scale, box, INK_LEVEL))
    return merged, (["tableformer_tables"] if len(merged) > len(grids) else [])


def _read_region(doc: Document, index: int, atoms: list[Atom], tables, built: PageLines | None, repairs_on: bool) -> dict:
    """Read one region of a text-layer page (the page, or one half of a spread): ruled tables,
    side-by-side rows, then lines. Blocks are appended in reading order, tables by their top edge."""
    t_blocks, body_atoms, in_tables, rejected, grids = table_blocks(index, atoms, tables, doc._rtl, repairs_on)
    if built is None or t_blocks:
        built = build_lines(body_atoms, default_rtl=doc._rtl)
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
            lines_built = built if len(segments) == 1 and segment.kind == "lines" and not t_blocks else build_lines(part, default_rtl=doc._rtl)
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
        tidy(table)  # D3g: learned proposals only, after arrange has extended them; ruled grids are never reshaped
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
        if "table_open_sides_inferred" in table.flags:
            # Put a form table with its own column, even if the preceding column runs lower.
            local = [(i, b.bbox) for i, b in enumerate(doc.blocks[first:], first)
                     if b.bbox and _same_ocr_column(b.bbox, table.bbox)]
            if local:
                at = next((i for i, box in local if box[1] >= table.bbox[3]), local[-1][0] + 1)
                doc.blocks.insert(at, table)
                continue
        at = len(doc.blocks)
        for i in range(first, len(doc.blocks)):
            block = doc.blocks[i]
            if block.bbox is not None and block.bbox[1] > table.bbox[1]:
                at = i
                break
        doc.blocks.insert(at, table)


def _render(page, frame=None) -> tuple[object, float]:
    """The page as a PIL image at RENDER_DPI, or lower when that would pass MAX_RENDER_PIXELS.

    Returns the image and the scale used (pixels per point). A huge or hostile page size
    cannot allocate billions of pixels.

    The page is rendered as displayed (``/Rotate`` applied), the frame its layer is read in.
    With ``frame`` for a page whose layer is sideways on screen (``Frame.sideways``), it is
    rendered unrotated, in the user frame its layer is read in, so layer boxes still meet it.
    """
    width, height = page.get_size()
    scale = RENDER_DPI / 72
    if width * height * scale * scale > MAX_RENDER_PIXELS:
        scale = (MAX_RENDER_PIXELS / (width * height)) ** 0.5
    rotation = (360 - frame.page_rotate) % 360 if frame is not None and frame.sideways else 0
    if rotation:
        return page.render(scale=scale, rotation=rotation).to_pil(), scale
    return page.render(scale=scale).to_pil(), scale


NUMBER_WORD_PSM = 8  # one word
NUMBER_WORD_PAD = 4  # pixels around a word's box
NUMBER_WORD_REACH = 3.0  # word heights the crop may reach into the blank beside a word
NUMBER_WORD_MIN_CONFIDENCE = 30.0
_NUMBER_WORD = re.compile(r"^[\d.,:/%₪()\-–'\"]*\d[\d.,:/%₪()\-–'\"]*$")
_HEBREW_LETTER = re.compile(r"[\u05d0-\u05ea]")


_LABEL_ONE_LEVEL = re.compile(r"^\d{1,2}\.?$")
_LABEL_DEEPER = re.compile(r"^\d{1,2}(\.\d{1,2})+\.?$")
# A deeper label read below NUMBER_WORD_MIN_CONFIDENCE is used only when it fits its neighbours' numbering.
LABEL_SEQUENCE_GUARD = True


def _number_crop(image, bbox) -> tuple[int, int, int, int] | None:
    x0, y0, x1, y1 = bbox
    crop = (max(0, int(x0) - NUMBER_WORD_PAD), max(0, int(y0) - NUMBER_WORD_PAD),
            min(image.size[0], int(x1) + NUMBER_WORD_PAD), min(image.size[1], int(y1) + NUMBER_WORD_PAD))
    return crop if crop[2] > crop[0] and crop[3] > crop[1] else None


def _number_reading(numbers, image, bbox, dpi: int, min_confidence: float = NUMBER_WORD_MIN_CONFIDENCE,
                    *, batch_results: dict | None = None) -> tuple[str, float] | None:
    """Return one number-shaped word above the specified confidence floor."""
    crop = _number_crop(image, bbox)
    if crop is None:
        return None
    second = batch_results.get(crop) if batch_results is not None else None
    if second is None:
        second = numbers.recognize(image.crop(crop), dpi=dpi, psm=NUMBER_WORD_PSM)
    words = [w for l in second.lines for w in l.words]
    if len(words) != 1 or words[0].confidence < min_confidence or not _NUMBER_WORD.match(words[0].text):
        return None
    return words[0].text, words[0].confidence


NUMBER_SEGMENT_MIN_CONFIDENCE = 50.0
_SEGMENT_WORD = re.compile(r"^[^\u05d0-\u05ea₪A-Za-z]+$")  # no Hebrew or Latin letter, no ₪: digits, punctuation, stray marks
_SEGMENT_DATE = re.compile(r"^\d{1,2}[./]\d{1,2}[./](?:\d{2}|\d{4})$")
SEGMENT_MAX_LETTERS = 2  # a word with a digit and at most this many Hebrew letters is a misread number piece ("0ך", "2ךד")


def _segment_piece(text: str) -> bool:
    """Whether an OCR word can be part of a number segment: digits, punctuation and marks; a ₪ alone
    (heb reads part of a date as one: "05.2024. ₪"); or a digit with at most SEGMENT_MAX_LETTERS Hebrew
    letters, one of them a final form, a misread number ("0ך", "2ךד"). "2ב" and "ו-3" are real and
    never are; nor are words of letters only ("של" in "בהיקף של 1,620")."""
    if re.search(r"[A-Za-z]", text):
        return False
    if _SEGMENT_WORD.match(text) or text.strip("()") == "₪":
        return True
    letters = _HEBREW_LETTER.findall(text)
    # A final form beside digits is no Hebrew: "2ב" (a sub-section) and "ו-3" are real, "0ך" is not.
    return (len(letters) <= SEGMENT_MAX_LETTERS and any(ch in "ךםןףץ" for ch in letters)
            and any(ch.isdigit() for ch in text) and "₪" not in text)
_SEGMENT_TOKEN = re.compile(r"^(?:\d{1,2}[./]\d{1,2}[./](?:\d{2}|\d{4})|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?%?|\d+(?:\.\d{1,2})?%?)$")


def _reread_number_segments(numbers, image, line, dpi: int) -> tuple[int, int, set]:
    """A line's number segments read again by the number model as one line (``--psm 7``). A segment
    is a stretch of consecutive number pieces (``_segment_piece``: digits, number punctuation, stray
    marks, a lone ₪, a digit misread with a final-form letter), holding a digit: the numeric cells at a table row's end, or an amount in a sentence. heb
    breaks dates there into word-sized pieces and marks ("01.12.2022" as "2022 >"), which neither a
    word nor a run read alone can restore. Each of heb's words goes to the number model's token over
    it; a token replaces the words it covers only when it is a whole number shape (a date, a grouped
    amount, a plain number, optionally with %), its confidence is at least
    ``NUMBER_SEGMENT_MIN_CONFIDENCE`` and it keeps their digits. Other words stay as heb read them.
    Tokens stay separate words, so numbers in neighbouring columns are never joined.
    Returns (segments read, segments replaced, ids of the words replaced)."""
    segments, current = [], []
    for word in line.words:
        if _segment_piece(word.text):
            current.append(word)
        else:
            if current:
                segments.append(current)
            current = []
    if current:
        segments.append(current)
    read = replaced = 0
    done: set = set()
    for segment in segments:
        digits = sum(ch.isdigit() for w in segment for ch in w.text)
        if not digits or len(segment) < 2:
            continue  # one word is left to its own second reading (labels, amounts)
        box = [min(w.bbox[0] for w in segment), min(w.bbox[1] for w in segment), max(w.bbox[2] for w in segment), max(w.bbox[3] for w in segment)]
        crop = (max(0, int(box[0]) - NUMBER_WORD_PAD), max(0, int(box[1]) - NUMBER_WORD_PAD),
                min(image.size[0], int(box[2]) + NUMBER_WORD_PAD), min(image.size[1], int(box[3]) + NUMBER_WORD_PAD))
        if crop[2] <= crop[0] or crop[3] <= crop[1]:
            continue
        result = numbers.recognize(image.crop(crop), dpi=dpi, psm=7)
        read += 1
        tokens = [w for l in result.lines for w in l.words if w.text.strip("|")]
        for t in tokens:  # into the line image's coordinates
            t.bbox = [t.bbox[0] + crop[0], t.bbox[1] + crop[1], t.bbox[2] + crop[0], t.bbox[3] + crop[1]]
        # Each of heb's words goes to the token over it (most overlap across); a token that is a whole
        # number shape, confident, and keeps the digits of the words it covers replaces them. A token
        # that is not (a mark, a misread digit at low confidence) leaves its words as heb read them.
        cover: dict[int, list] = {}
        for w in segment:
            best = max(tokens, key=lambda t: min(w.bbox[2], t.bbox[2]) - max(w.bbox[0], t.bbox[0]), default=None)
            if best is not None and min(w.bbox[2], best.bbox[2]) - max(w.bbox[0], best.bbox[0]) > 0:
                cover.setdefault(id(best), []).append(w)
        changes = []
        for t in tokens:
            covered = cover.get(id(t), [])
            if not covered or not _SEGMENT_TOKEN.match(t.text) or t.confidence < NUMBER_SEGMENT_MIN_CONFIDENCE:
                continue
            if sum(ch.isdigit() for ch in t.text) < sum(ch.isdigit() for w in covered for ch in w.text):
                continue
            if any("₪" in w.text for w in covered) and not _SEGMENT_DATE.match(t.text):
                continue  # only a date may take a ₪'s place: an amount keeps its currency
            if [w.text for w in covered] == [t.text]:
                done.update(id(w) for w in covered)
                continue
            changes.append((t, covered))
        if not changes:
            continue
        first = " ".join(w.text for w in segment)
        for t, covered in changes:
            at = next((i for i, w in enumerate(line.words) if w is covered[0]), None)
            if at is None:
                continue
            new_word = type(covered[0])(t.text, t.bbox, t.confidence)
            line.words[:] = [w for w in line.words if not any(w is c for c in covered)]
            line.words.insert(at, new_word)
            done.add(id(new_word))
        line.number_readings.append({"first": first, "second": " ".join(t.text for t, _ in changes),
                                     "first_confidence": min(w.confidence for w in segment),
                                     "second_confidence": round(sum(t.confidence for t, _ in changes) / len(changes), 2),
                                     "model": numbers.lang, "wider": False, "segment": len(segment),
                                     "replaced": [[" ".join(w.text for w in covered), t.text] for t, covered in changes]})
        line.numbers_replaced += 1
        replaced += 1
    if replaced:
        line.text = " ".join(w.text for w in line.words)
    return read, replaced, done


def _rtl_order(words) -> bool:
    """Whether a line's words run right to left (heb's reading order): each starts left of the one before."""
    xs = [w.bbox[0] for w in words]
    return len(xs) > 1 and sum(b < a for a, b in zip(xs, xs[1:])) >= sum(b > a for a, b in zip(xs, xs[1:]))


NUMBER_RUN_GAP = 0.6  # line heights: two pieces of one number are closer than this, and than the line's median word gap
# What a joined run must read as: a whole date, or a grouped amount (optional decimals or %).
_RUN_READING = re.compile(r"^(?:\d{1,2}[./]\d{1,2}[./](?:\d{2}|\d{4})|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?%?)$")
_RUN_PIECE = re.compile(r"^[\d.,:/%()\-–'\"*!?<>|]+$")  # digits, number punctuation, or OCR's stray marks: no letter, no ₪


def _reread_number_runs(numbers, image, line, dpi: int, skip: set = frozenset()) -> tuple[int, int, set]:
    """Runs of adjacent number-like words in one OCR line, read again as one crop. heb splits a date
    or an amount into pieces ("25.10.2022" as "2022 25.", "385,000" as "385 ,000"), and each piece read
    alone by the number model cannot restore it. A run of two or more words of digits and number
    punctuation only (stray marks between them included; no letter, no ₪, which heb reads and the
    number model cannot), starting and ending with a digit, each less than
    ``NUMBER_RUN_GAP`` line heights from the next, is read by the number model over their joined box,
    and replaced by that reading when it is one number-shaped word that keeps every digit of the run.
    Returns (runs read, runs replaced, ids of the words read)."""
    words = line.words

    def gap_between(a, b) -> float:
        return max(b.bbox[0] - a.bbox[2], a.bbox[0] - b.bbox[2])

    gaps = sorted(gap_between(a, b) for a, b in zip(words, words[1:]))
    median_gap = gaps[len(gaps) // 2] if gaps else 0.0
    runs, current = [], []
    for word in words:
        fits = bool(_RUN_PIECE.match(word.text)) and id(word) not in skip
        if fits and current:
            prev = current[-1]
            height = max(word.bbox[3] - word.bbox[1], prev.bbox[3] - prev.bbox[1], 1.0)
            # Closer than a normal word space: two numbers in neighbouring columns of an unruled table
            # stand a column gap apart, and are never joined.
            if gap_between(prev, word) > min(NUMBER_RUN_GAP * height, median_gap):
                runs.append(current)
                current = []
        if fits:
            current.append(word)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    read = replaced = 0
    done: set = set()
    for run in runs:
        # A run starts and ends with a piece that holds a digit: stray marks are taken in only between pieces.
        while run and not any(ch.isdigit() for ch in run[0].text):
            run = run[1:]
        while run and not any(ch.isdigit() for ch in run[-1].text):
            run = run[:-1]
        if len(run) < 2:
            continue
        box = [min(w.bbox[0] for w in run), min(w.bbox[1] for w in run), max(w.bbox[2] for w in run), max(w.bbox[3] for w in run)]
        found = _number_reading(numbers, image, box, dpi)
        read += 1
        if found is None:
            continue  # the run's words are read one by one instead
        reading, confidence = found
        if not _RUN_READING.match(reading):
            continue  # only a whole date or a grouped amount replaces pieces
        if sum(ch.isdigit() for ch in reading) < sum(ch.isdigit() for w in run for ch in w.text):
            continue  # a reading that drops digits is no repair
        done.update(id(w) for w in run)
        first = " ".join(w.text for w in run)
        line.number_readings.append({"first": first, "second": reading, "first_confidence": min(w.confidence for w in run),
                                     "second_confidence": confidence, "model": numbers.lang, "wider": False, "run": len(run)})
        keep = run[0]
        keep.text, keep.bbox, keep.confidence = reading, box, confidence
        line.words[:] = [w for w in line.words if w is keep or not any(w is r for r in run)]
        line.numbers_replaced += 1
        replaced += 1
    if replaced:
        line.text = " ".join(w.text for w in line.words)
    return read, replaced, done


def _is_number_word_candidate(word) -> bool:
    return any(ch.isdigit() for ch in word.text) and not _HEBREW_LETTER.search(word.text)


def _batch_number_words(numbers, image, result, dpi: int) -> dict[tuple[int, int, int, int], OcrResult]:
    # Corrections can change word boxes. Only reuse readings for identical padded crops.
    crops = dict.fromkeys(
        crop
        for line in result.lines
        for word in line.words
        if _is_number_word_candidate(word) and (crop := _number_crop(image, word.bbox)) is not None
    )
    pending = list(crops)
    readings = {}
    for offset in range(0, len(pending), 64):
        group = pending[offset:offset + 64]
        if len(group) < 2:
            break
        try:
            batch = numbers.recognize_batch((image.crop(crop) for crop in group), dpi, psm=NUMBER_WORD_PSM)
            if len(batch) != len(group):
                break
        except (OcrUnavailable, OSError, ValueError):
            # Preserve serial retries and their normal failure accounting.
            break
        readings.update(zip(group, batch, strict=True))
    return readings


def _reread_number_words(numbers, image, result, dpi: int, *, batch: bool = False) -> tuple[int, int]:
    """A second reading of the words of an OCR result that hold digits and no Hebrew letter, each on
    its own crop, by the number model (``eng``): stock ``heb`` misreads digits, drops the levels of a
    label ("1.1." as "1.") and splits amounts. A word is replaced when the second reading is itself
    number-shaped (digits and number punctuation only) with confidence at least
    ``NUMBER_WORD_MIN_CONFIDENCE``. Words with Hebrew letters ("2ב", "ו-3") keep their first reading.
    ``result``'s lines are updated in place. Returns (words read again, words replaced)."""
    reread = replaced = 0
    pending: dict[int, tuple[str, float]] = {}
    batch_results = _batch_number_words(numbers, image, result, dpi) if batch else None
    for line in result.lines:
        changed = False
        try:
            segs_read, segs_replaced, seg_done = _reread_number_segments(numbers, image, line, dpi)
            runs_read, runs_replaced, done = _reread_number_runs(numbers, image, line, dpi, skip=seg_done)
            done |= seg_done
            runs_read += segs_read
            runs_replaced += segs_replaced
        except OcrUnavailable:
            return reread, replaced
        reread += runs_read
        replaced += runs_replaced
        changed = runs_replaced > 0
        for word in line.words:
            if id(word) in done or not _is_number_word_candidate(word):
                continue
            wider = levels = False
            at_start = word is max(line.words, key=lambda w: w.bbox[2])
            # "4." or "1.2." (a label with its stop, not a bare "4"): heb may keep only the last levels.
            label = at_start and bool(_LABEL_ONE_LEVEL.match(word.text) or _LABEL_DEEPER.match(word.text)) and word.text.endswith(".")
            try:
                found = _number_reading(numbers, image, word.bbox, dpi,
                                        min_confidence=0.0 if label else NUMBER_WORD_MIN_CONFIDENCE,
                                        batch_results=batch_results)
                reread += 1
                reading, confidence = found if found is not None else (None, None)
                if reading is not None and confidence < NUMBER_WORD_MIN_CONFIDENCE:
                    # heb cuts a deeper label at a line's (RTL) start to its last levels ("2.2.4." as "4.",
                    # "11.1.2." as "1.2."); the number model reads it whole, often with a low confidence on such
                    # short crops. Below the floor its reading is kept only when it is a deeper label ending
                    # with all of heb's levels.
                    levels = bool(_LABEL_DEEPER.match(reading)) and reading.rstrip(".").endswith("." + word.text.rstrip("."))
                    if not levels:
                        reading = confidence = None
                    elif LABEL_SEQUENCE_GUARD:
                        pending[id(word)] = (reading, confidence)
                        continue  # decided with its neighbours, once the page's lines are read
                if reading is not None and _LABEL_ONE_LEVEL.match(reading) and at_start:
                    # A one-level label at a line's (RTL) start: heb's box often covers only the part of a
                    # deeper label it read ("1." of "1.1.1."). One more reading reaches into the blank
                    # beside it, up to the next word, and is used only when it is a deeper label.
                    reach = NUMBER_WORD_REACH * (word.bbox[3] - word.bbox[1])
                    left = max((w.bbox[2] for w in line.words if w is not word and w.bbox[2] <= word.bbox[0]), default=word.bbox[0] - reach)
                    wide = [max(word.bbox[0] - reach, left + NUMBER_WORD_PAD), word.bbox[1], word.bbox[2] + reach, word.bbox[3]]
                    deeper = _number_reading(numbers, image, wide, dpi)
                    # heb keeps a label's last level ("1." of "3.2.1."): the deeper reading must end with it.
                    if deeper is not None and _LABEL_DEEPER.match(deeper[0]) and deeper[0].rstrip(".").endswith(reading.rstrip(".")):
                        reading, confidence = deeper
                        wider = True
                if label and LABEL_CROPS and _LABEL_ONE_LEVEL.match(word.text) and (reading is None or not _LABEL_DEEPER.match(reading)) and id(word) not in pending:
                    # heb's box over a cut label often reads as noise to the number model too ("iWeeg" for
                    # ".1.1.1"). A few more crops (contrast stretched, and reaching left into the label's
                    # dropped levels) usually hold it: the deeper label most of them read, ending with heb's
                    # level, is decided by its neighbours like any low-confidence one.
                    found = _label_from_crops(numbers, image, word, dpi)
                    if found is not None:
                        pending[id(word)] = (found[0], found[1], False, True)
                        continue
            except OcrUnavailable:
                return reread, replaced
            if reading is None:
                continue
            if word.text[-1] in ".:" and reading[-1].isdigit():
                reading += word.text[-1]  # eng drops a label's closing stop more often than heb invents one
            if reading != word.text:
                # Every replacement is logged with both readings (decision 0002), as a cell's are: a
                # reviewer can audit it, a consumer can revert it.
                line.number_readings.append({"first": word.text, "second": reading, "first_confidence": word.confidence,
                                             "second_confidence": confidence, "model": numbers.lang, "wider": wider,
                                             **({"label_levels": True} if levels else {})})
                word.text = reading
                line.numbers_replaced += 1
                replaced += 1
                changed = True
        if changed:
            line.text = " ".join(w.text for w in line.words)
    if pending:
        replaced += _label_sequence(result.lines, pending, numbers.lang)
    return reread, replaced


LABEL_CROPS = True  # a cut label heb's box reads as noise is read again on a few more crops
# A crop candidate vouched for only by another candidate (LABEL_CHAIN) needs most crops to agree on it:
# a reading that only one or two crops give is too often wrong.
LABEL_CROPS_CHAIN_MIN_SHARE = 60.0
_LABEL_IN_READING = re.compile(r"\d{1,2}(?:\.\d{1,2})+")


def _label_from_crops(numbers, image, word, dpi: int) -> tuple[str, float] | None:
    """The deeper label most of a few crops of a cut label read, as (reading, share of crops that read
    it x 100); None when no crop reads a deeper label ending with heb's level. The crops: heb's box
    with its contrast stretched, and boxes reaching one and two line heights left (where an RTL label's
    dropped levels sit), plain and stretched. A reading may carry noise around the label ("Yo.1.1.1"):
    the label is searched in it."""
    from PIL import ImageOps

    x0, y0, x1, y1 = word.bbox
    h = y1 - y0
    level = "." + word.text.rstrip(".")
    found = []
    boxes = [((x0 - NUMBER_WORD_PAD, y0 - NUMBER_WORD_PAD, x1 + NUMBER_WORD_PAD, y1 + NUMBER_WORD_PAD), True)]
    boxes += [((x0 - k * h, y0 - 8, x1 + 8, y1 + 8), stretch) for k in (1, 2) for stretch in (False, True)]
    for (bx0, by0, bx1, by1), stretch in boxes:
        box = (max(0, int(bx0)), max(0, int(by0)), min(image.size[0], int(bx1)), min(image.size[1], int(by1)))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        crop = image.crop(box).convert("L")
        if stretch:
            crop = ImageOps.autocontrast(crop, cutoff=1)
        result = numbers.recognize(crop, dpi=dpi, psm=7)
        text = "".join(w.text for line in result.lines for w in line.words if w.text.strip("|"))
        match = _LABEL_IN_READING.search(text)
        if match and ("." + match.group()).endswith(level):
            found.append(match.group() + ".")
    if not found:
        return None
    best, count = Counter(found).most_common(1)[0]
    return best, round(100.0 * count / len(boxes), 1)


def _label_levels(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.rstrip(".").split("."))


def _label_follows(prev: tuple[int, ...], nxt: tuple[int, ...]) -> bool:
    """Whether label ``nxt`` can follow ``prev`` in a numbered list: a deeper level of it ("2.2" then
    "2.2.4"), or the next number at one of its levels ("2.1.3" then "2.2", "2.3.5" then "3.1")."""
    k = next((i for i, (a, b) in enumerate(zip(prev, nxt)) if a != b), min(len(prev), len(nxt)))
    if k == len(prev):
        return len(nxt) > len(prev)
    return k < len(nxt) and nxt[k] == prev[k] + 1


def _label_starts_numbering(prev: tuple[int, ...], nxt: tuple[int, ...]) -> bool:
    """The strict form of ``_label_follows``, for two candidates that vouch for each other: a deeper
    label starts its numbering at 1 ("2.2" then "2.2.1"), and the next number at a level restarts the
    levels below it ("2.1.3" then "2.2" or "2.2.1", not "2.2.4")."""
    k = next((i for i, (a, b) in enumerate(zip(prev, nxt)) if a != b), min(len(prev), len(nxt)))
    if k == len(prev):
        return len(nxt) > len(prev) and all(x == 1 for x in nxt[k:])
    return k < len(nxt) and nxt[k] == prev[k] + 1 and all(x == 1 for x in nxt[k + 1 :])


LABEL_CHAIN = True  # two neighbouring candidates in strict sequence vouch for each other
LABEL_CARRY = True  # ... also through the cut labels between them, by the last levels heb kept
LABEL_CARRY_MAX = 16  # labels the numbering may allow at once on the way; more, and nothing is vouched for


def _cut_label_level(text: str) -> int | None:
    """The last level of a label at a line's start ("4." of a cut "2.2.4.", or of "2.2.4." itself), else None."""
    if not text.endswith(".") or not (_LABEL_ONE_LEVEL.match(text) or _LABEL_DEEPER.match(text)):
        return None
    return _label_levels(text)[-1]


def _label_steps(prev: tuple[int, ...], last: int) -> set[tuple[int, ...]]:
    """The labels that can follow ``prev`` in strict sequence (``_label_starts_numbering``) and end with
    level ``last``: one level deeper ("2.2" then "2.2.1"), or the next number at one of its levels
    ("1.3.2" then "1.4", "2" or "1.3.3"), possibly opening deeper levels at 1 ("2.1.3" then "2.2.1")."""
    out: set[tuple[int, ...]] = set()
    if last == 1:
        out.add(prev + (1,))
    for k, value in enumerate(prev):
        if value + 1 == last:
            out.add(prev[:k] + (last,))
        if last == 1:
            out.update(prev[:k] + (value + 1,) + (1,) * m for m in range(1, len(prev) - k))
    return out


def _label_reaches(first: tuple[int, ...], second: tuple[int, ...], lasts: list[int | None]) -> bool:
    """Whether label ``second`` follows ``first`` in strict sequence through the label lines between,
    whose last levels are ``lasts`` (lines without a label, None, are passed over). There must be at
    least one: with none, this is ``_label_starts_numbering`` between neighbours, and adds nothing."""
    if all(last is None for last in lasts):
        return False
    possible = {first}
    for last in lasts:
        if last is None:
            continue
        possible = {nxt for prev in possible for nxt in _label_steps(prev, last)}
        if not possible or len(possible) > LABEL_CARRY_MAX:
            return False
    return any(second in _label_steps(prev, second[-1]) for prev in possible)


def _label_sequence(lines, pending: dict[int, tuple[str, float]], model: str) -> int:
    """Decide the low-confidence deeper labels (``pending``: word id -> (reading, confidence)) by their
    neighbours. Lines are taken in reading order; a line's first (rightmost) word is a known label when
    it is already a deeper label. A candidate is used when it can follow the last label before it (known
    or used), or when the next known label can follow it ("23.2" between "3.1" and "3.3" cannot).
    On a page whose labels are all cut there is no known label: with ``LABEL_CHAIN``, a candidate is
    also used when the nearest label line before or after it is another candidate in strict sequence
    with it (``_label_starts_numbering``: "1.1.1" then "1.1.2", not "3.1.8" then "3.2.9"); such a use
    is logged with ``chained: true``. With ``LABEL_CARRY``, the two may also stand apart, with cut labels
    between them: stepping strictly from one through the last levels heb kept on those lines must reach
    the other ("1.4." then a cut "1." then "1.4.2."). That evidence is more than two neighbours', so it
    holds for crop candidates few crops agree on too; such a use is logged ``through_cut_labels: true``.
    Otherwise heb's reading stays, and the rejected reading is logged with ``used: false``.
    Returns the number of words replaced."""
    starts = [(line, max(line.words, key=lambda w: w.bbox[2])) for line in lines if line.words]
    known = [_label_levels(w.text) if id(w) not in pending and _LABEL_DEEPER.match(w.text) else None for _, w in starts]
    candidate = [_label_levels(pending[id(w)][0]) if id(w) in pending else None for _, w in starts]
    labelled = [i for i in range(len(starts)) if known[i] or candidate[i]]
    replaced, previous = 0, None
    for i, (line, word) in enumerate(starts):
        if id(word) not in pending:
            previous = known[i] or previous
            continue
        reading, confidence, *extra = pending[id(word)]
        from_crops = len(extra) > 1 and extra[1]
        levels = _label_levels(reading)
        following = next((k for k in known[i + 1 :] if k), None)
        used = (previous is not None and _label_follows(previous, levels)) or (following is not None and _label_follows(levels, following))
        chained = carried = False
        if not used and LABEL_CHAIN:
            at = labelled.index(i)
            before = candidate[labelled[at - 1]] if at > 0 else None
            after = candidate[labelled[at + 1]] if at + 1 < len(labelled) else None
            if not from_crops or confidence >= LABEL_CROPS_CHAIN_MIN_SHARE:
                chained = (before is not None and _label_starts_numbering(before, levels)) or (after is not None and _label_starts_numbering(levels, after))
            if not chained and LABEL_CARRY:
                # The cut labels between two candidates kept their last levels, which heb reads right:
                # stepping through them, strictly, from one candidate must reach the other.
                lasts = lambda a, b: [_cut_label_level(w.text) for _, w in starts[a + 1 : b]]
                chained = (before is not None and _label_reaches(before, levels, lasts(labelled[at - 1], i))) or (
                    after is not None and _label_reaches(levels, after, lasts(i, labelled[at + 1])))
                carried = chained
            used = chained
        if word.text[-1] in ".:" and reading[-1].isdigit():
            reading += word.text[-1]
        entry = {"first": word.text, "second": reading, "first_confidence": word.confidence, "second_confidence": confidence,
                 "model": model, "wider": False, "label_levels": True, **({"crops": True} if from_crops else {})}
        if not used:
            line.number_readings.append({**entry, "used": False})
            continue
        line.number_readings.append({**entry, **({"chained": True} if chained else {}), **({"through_cut_labels": True} if carried else {})})
        word.text = reading
        line.numbers_replaced += 1
        line.text = " ".join(w.text for w in line.words)
        replaced += 1
        previous = levels
    return replaced


PAGE_NUMBER_PASS = True  # one more reading of each OCR page by the number model, for the numbers heb cut or misread
PAGE_NUMBER_DPI = 150  # the page pass reads at this resolution: eng's page segmentation finds more numbers there than at 300
PAGE_NUMBER_MIN_OVERLAP = 0.5  # a token covers a word when it overlaps this share of the lower one's height


# A cut label's deeper reading that the sequence guard turned down on the word's crop, and that the page
# pass reads the same, is used: two readings of different images (the word's crop at the page's dpi, the
# whole page at PAGE_NUMBER_DPI) that agree on every level are evidence of their own, where the
# neighbours cannot vouch because their labels are cut too. Agreement stands in for a neighbour that is
# missing, never for one that contradicts: a known label before it that it cannot follow, or one after
# it that cannot follow it, keeps the reading out (both readings can see the same stray ink: "2." beside
# a mark read "9.2.", after "1.2.").
LABEL_AGREEMENT = True


def _labels_agreed(lines, pending: dict, model: str) -> int:
    """Use the pending page-pass labels (word id -> (reading, confidence)) that a rejected crop reading
    of the same word agrees with; they leave ``pending``. Each use is logged with ``agreed: true``."""
    replaced = 0
    starts = [(line, max(line.words, key=lambda w: w.bbox[2])) for line in lines if line.words]
    known = [_label_levels(w.text) if id(w) not in pending and _LABEL_DEEPER.match(w.text) else None for _, w in starts]
    for i, (line, word) in enumerate(starts):
        if id(word) not in pending:
            continue
        reading, confidence = pending[id(word)][:2]
        levels = _label_levels(reading)
        previous = next((k for k in reversed(known[:i]) if k), None)
        following = next((k for k in known[i + 1 :] if k), None)
        if (previous is not None and not _label_follows(previous, levels)) or (following is not None and not _label_follows(levels, following)):
            continue  # a known neighbour contradicts it: agreement does not override the sequence
        crop = next((e for e in line.number_readings if e.get("used") is False and not e.get("page_pass")
                     and e.get("first") == word.text and e.get("second", "").rstrip(".") == reading.rstrip(".")), None)
        if crop is None:
            continue
        del pending[id(word)]
        if word.text[-1] in ".:" and reading[-1].isdigit():
            reading += word.text[-1]
        line.number_readings.append({"first": word.text, "second": reading, "first_confidence": word.confidence,
                                     "second_confidence": confidence, "model": model, "wider": False,
                                     "label_levels": True, "page_pass": True, "agreed": True})
        word.text = reading
        line.numbers_replaced += 1
        line.text = " ".join(w.text for w in line.words)
        replaced += 1
    return replaced


def _page_number_pass(numbers, image, result, dpi: int) -> tuple[tuple[int, int], tuple[OcrResult, tuple[int, int]]]:
    """The whole page read once more by the number model (``--psm 3`` at ``PAGE_NUMBER_DPI``), for the
    numbers no crop restored: heb cuts a label at a line's start to its last level ("1." of "2.3.1.")
    and reads a short line's digits apart, where a crop of heb's box holds only what heb saw. Its
    tokens never add text (eng finds numbers in stamps, rulings and Hebrew letters): each of heb's
    number pieces (``_segment_piece``, with a digit) goes to the token over it, and a token replaces
    the pieces it covers only on the rules of ``_reread_number_segments`` (a whole number shape,
    confidence at least ``NUMBER_SEGMENT_MIN_CONFIDENCE``, every digit kept, a ₪ only by a date), and
    two more: it adds digits only as a date or grouped amount (``_RUN_READING``), and it changes heb's
    digits only when it is surer than heb's words. A
    deeper label at a line's start replaces heb's label when it ends with heb's levels, at any
    confidence, and only through the sequence guard (``_label_sequence``). Each replacement is logged
    with ``page_pass: true``. Returns the number counts, plus the English reading and its raster size."""
    from PIL import Image

    factor = PAGE_NUMBER_DPI / dpi
    small = image if factor >= 1 else image.resize((max(1, round(image.size[0] * factor)), max(1, round(image.size[1] * factor))), Image.LANCZOS)
    factor = small.size[0] / image.size[0]
    english = numbers.recognize(small, dpi=round(dpi * factor), psm=3)
    tokens = [OcrWord(w.text, [v / factor for v in w.bbox], w.confidence)
              for line in english.lines for w in line.words if any(ch.isdigit() for ch in w.text)]

    def overlap(w, t) -> float:
        across = min(w.bbox[2], t.bbox[2]) - max(w.bbox[0], t.bbox[0])
        down = min(w.bbox[3], t.bbox[3]) - max(w.bbox[1], t.bbox[1])
        if across <= 0 or down < PAGE_NUMBER_MIN_OVERLAP * min(w.bbox[3] - w.bbox[1], t.bbox[3] - t.bbox[1]):
            return 0.0
        return across * down

    found = replaced = 0
    pending: dict[int, tuple[str, float]] = {}
    for line in result.lines:
        if not line.words:
            continue
        start = max(line.words, key=lambda w: w.bbox[2])
        cover: dict[int, list] = {}
        by_id = {}
        for w in line.words:
            if not (_segment_piece(w.text) and any(ch.isdigit() for ch in w.text)):
                continue
            best = max(tokens, key=lambda t: overlap(w, t), default=None)
            if best is not None and overlap(w, best) > 0:
                cover.setdefault(id(best), []).append(w)
                by_id[id(best)] = best
        changes = []
        for key, covered in cover.items():
            t = by_id[key]
            found += 1
            text = t.text.strip("|")
            if [w.text for w in covered] == [text]:
                continue
            label = (covered == [start] and start.text.endswith(".") and bool(_LABEL_ONE_LEVEL.match(start.text) or _LABEL_DEEPER.match(start.text)))
            if label:
                reading = text if text.endswith(".") else text + "."
                if _LABEL_DEEPER.match(reading) and reading.rstrip(".").endswith("." + start.text.rstrip(".")):
                    if LABEL_SEQUENCE_GUARD:
                        pending[id(start)] = (reading, t.confidence)
                    else:
                        changes.append((t, reading, covered))
                continue
            if not _SEGMENT_TOKEN.match(text) or t.confidence < NUMBER_SEGMENT_MIN_CONFIDENCE:
                continue
            digits = "".join(ch for ch in text if ch.isdigit())
            first = "".join(ch for w in covered for ch in w.text if ch.isdigit())
            if len(digits) < len(first):
                continue
            if len(digits) > len(first) and not _RUN_READING.match(text):
                continue  # eng adds digits from nearby ink to a plain number ("60" as "6019"); a date or amount heb broke is restored
            if digits != first and len(digits) == len(first) and t.confidence <= min(w.confidence for w in covered):
                continue  # at 150 dpi eng confuses 3 and 5: a digit heb read surer than eng stays
            if any("₪" in w.text for w in covered) and not _SEGMENT_DATE.match(text):
                continue  # only a date may take a ₪'s place: an amount keeps its currency
            changes.append((t, text, covered))
        for t, text, covered in changes:
            at = next((i for i, w in enumerate(line.words) if w is covered[0]), None)
            if at is None:
                continue
            line.number_readings.append({"first": " ".join(w.text for w in covered), "second": text,
                                         "first_confidence": min(w.confidence for w in covered), "second_confidence": t.confidence,
                                         "model": numbers.lang, "wider": False, "page_pass": True})
            line.words[:] = [w for w in line.words if not any(w is c for c in covered)]
            line.words.insert(at, type(covered[0])(text, list(t.bbox), t.confidence))
            line.numbers_replaced += 1
            replaced += 1
        if changes:
            line.text = " ".join(w.text for w in line.words)
    if pending and LABEL_AGREEMENT:
        replaced += _labels_agreed(result.lines, pending, numbers.lang)
    if pending:
        first = {id(line): len(line.number_readings) for line in result.lines}
        replaced += _label_sequence(result.lines, pending, numbers.lang)
        for line in result.lines:
            for entry in line.number_readings[first[id(line)]:]:
                entry["page_pass"] = True
    return (found, replaced), (english, small.size)


def _same_ocr_column(box, table) -> bool:
    """A paragraph and a form table share most of their narrower horizontal span."""
    overlap = min(box[2], table[2]) - max(box[0], table[0])
    return overlap >= 0.65 * min(box[2] - box[0], table[2] - table[0])


@dataclass
class _OcrLineRecords:
    by_line: dict[int, OcrLine] = field(default_factory=dict)
    attached: set[int] = field(default_factory=set)


def _ocr_block(index, engine, lines, group, dpi, flags, attached) -> Block:
    """Project the line readings and their aggregate checks onto one output block."""
    text = "\n".join(line.text for line in lines)
    replaced = sum(line.numbers_replaced for line in group)
    latin = sum(line.latin_runs_reread for line in group)
    return Block(
        kind="text",
        text=text,
        page=index,
        bbox=[min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines), max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)],
        source="ocr",
        tool=engine.version,
        direction="rtl",
        lines=lines,
        ocr={**block_ocr(engine, group), "dpi": dpi, **({"number_words_replaced": replaced} if replaced else {}),
             **({"latin_runs_reread": latin} if latin else {})},
        checks={"number_readings": [r for line in group for r in line.number_readings]} if replaced or any(line.number_readings for line in group) else None,
        flags=["ocr_unverified", *flags] + (["render_dpi_reduced"] if dpi < RENDER_DPI else [])
        + (["numbers_from_second_reading"] if replaced else [])
        + (["latin_from_second_reading"] if latin else [])
        + (["reading_order_ocr_margin_numbers_unverified"] if any(id(line) in attached for line in group) else [])
        + text_flags(text),
    )


def _ocr_blocks(
    doc: Document, index: int, engine, result, render_scale: float, *, offset=(0.0, 0.0), flags=(),
    min_confidence: float | None = None, form_tables=(), margin_page: tuple[int, int] | None = None,
    line_records: _OcrLineRecords | None = None,
) -> int:
    """OCR lines into blocks, one per Tesseract paragraph, with boxes in page points. Returns characters.

    With ``min_confidence``, paragraphs below it are left out; the caller records them as unread.
    """
    scale = 1 / render_scale
    dpi = round(render_scale * 72, 1)
    groups: list[list] = []
    for line in result.lines:
        # With the grid masked, OCR may put the instructions above and below it in the same
        # paragraph. Split at the bottom of that grid so its block can be inserted between them.
        if (groups and groups[-1][-1].paragraph == line.paragraph
                and not any(groups[-1][-1].bbox[1] * scale < table[3] <= line.bbox[1] * scale
                            and _same_ocr_column([v * scale for v in line.bbox], table) for table in form_tables)):
            groups[-1].append(line)
        else:
            groups.append([line])
    read = 0
    if min_confidence is not None:
        groups = [g for g in groups if (block_ocr(engine, g)["mean_confidence"] or 0) >= min_confidence]
    attached = set()
    if margin_page is not None:
        groups, attached = attach_margin_numbers(groups, margin_page)
    for group in groups:
        lines = [
            Line(line.text, [offset[0] + v * scale if i % 2 == 0 else offset[1] + v * scale for i, v in enumerate(line.bbox)])
            for line in group
        ]
        block = _ocr_block(index, engine, lines, group, dpi, flags, attached)
        read += sum(not ch.isspace() for ch in block.text)
        doc.blocks.append(block)
        if line_records is not None:
            line_records.by_line.update((id(line), reading) for line, reading in zip(lines, group))
    if line_records is not None:
        line_records.attached = attached
    return read


def _order_ocr_gaps(blocks, first, index, engine, scale, records) -> bool:
    # The margin pass deliberately puts detached clause numbers beside their targets.
    if records.attached:
        return False
    body = blocks[first:]
    ordered = order_across_disjoint_gaps(body)
    if ordered is None:
        return False
    dpi = round(scale * 72, 1)
    rebuilt = []
    for origin, run in groupby(ordered, key=lambda entry: entry[0]):
        lines = [line for _, line in run]
        old = body[origin]
        if lines == old.lines:
            rebuilt.append(old)
        else:
            readings = [records.by_line[id(line)] for line in lines]
            rebuilt.append(_ocr_block(index, engine, lines, readings, dpi, (), records.attached))
    blocks[first:] = rebuilt
    return True


def _unrotate_records(doc: Document, first_block: int, first_unread: int, index: int, angle: float, center) -> None:
    """Boxes a deskewed page recorded (its blocks, their lines, its unread entries), turned back
    into the page's own coordinates."""
    for block in doc.blocks[first_block:]:
        if block.page != index:
            continue
        if block.bbox:
            block.bbox = unrotate_box(block.bbox, angle, center)
        for line in block.lines or []:
            line.bbox = unrotate_box(line.bbox, angle, center)
    for entry in doc.unread[first_unread:]:
        if entry.page == index and entry.bbox:
            entry.bbox = unrotate_box(entry.bbox, angle, center)


def _order_ocr_paragraphs(blocks: list[Block], first: int) -> bool:
    """Put consecutive, well-aligned paragraphs in the same visual column in vertical order.

    Require a common horizontal span of at least two thirds of the widest paragraph.
    Overlapping vertical boxes may be different flows, so keep their engine order. A small
    overlap is allowed for OCR boxes that include ascenders, descenders and stray marks.
    """
    changed = False

    def close(start: int, end: int) -> None:
        nonlocal changed
        if end - start < 2:
            return
        run = blocks[start:end]
        ordered = sorted(run, key=lambda b: b.bbox[1])
        if ordered == run:
            return
        for upper, lower in pairwise(ordered):
            a, b = upper.bbox, lower.bbox
            if a[3] - b[1] > 0.15 * min(a[3] - a[1], b[3] - b[1]):
                return
        blocks[start:end] = ordered
        changed = True

    start = first
    left = right = widest = 0.0
    for i in range(first, len(blocks)):
        x0, _, x1, _ = blocks[i].bbox
        common_left, common_right = max(left, x0), min(right, x1)
        width = max(widest, x1 - x0)
        if i > start and common_right - common_left >= (2 / 3) * width:
            left, right, widest = common_left, common_right, width
        else:
            close(start, i)
            start, left, right, widest = i, x0, x1, x1 - x0
    close(start, len(blocks))
    return changed


def _order_ocr_bands(blocks: list[Block], first: int, image, scale: float) -> bool:
    """Finish a set of columns before a clearly separated lower part of a scan.

    A separator must be a long printed rule through a gap between the OCR blocks, below
    substantial side-by-side prose. Retain engine order within either side of the rule.
    """
    body = blocks[first:]
    if len(body) < 4 or all(a.bbox[1] <= b.bbox[1] for a, b in pairwise(body)):
        return False
    width, height = (v / scale for v in image.size)
    horizontal, _ = image_rulings(image, scale)
    cuts = []
    for rule in horizontal:
        if rule.end - rule.start < 0.70 * width or not 0.18 * height <= rule.pos <= 0.82 * height:
            continue
        above = [b for b in body if b.bbox[3] <= rule.pos]
        below = [b for b in body if b.bbox[1] >= rule.pos]
        # A block that straddles the rule makes its reading relation ambiguous.
        if len(above) < 2 or len(below) < 2 or len(above) + len(below) != len(body):
            continue
        middle = (rule.start + rule.end) / 2
        left = [b.bbox for b in above if len(b.lines) >= 3 and b.bbox[2] <= middle]
        right = [b.bbox for b in above if len(b.lines) >= 3 and b.bbox[0] >= middle]
        if any(min(a[3], b[3]) - max(a[1], b[1]) >= 0.5 * min(a[3] - a[1], b[3] - b[1])
               for a in left for b in right):
            cuts.append(rule.pos)
    if not cuts:
        return False
    cuts.sort()
    ordered = sorted(body, key=lambda b: bisect_right(cuts, b.bbox[1]))
    if ordered == body:
        return False
    blocks[first:] = ordered
    return True


# Ink in the rest of a page, once its tables are taken out, is looked for tile by tile: a short faint line
# holds far less ink than a share of a whole page, and was dropped. Bare grainy paper has no tile with a
# pixel REST_FAINT_DELTA levels under the paper; a faint line's tile has hundreds.
REST_FAINT_DELTA = 25
REST_TILE_PT = 30.0


def _rest_has_ink(image, scale: float) -> bool:
    """Whether some tile of ``image`` holds ``MIN_INK_PIXELS`` pixels ``REST_FAINT_DELTA`` levels under its paper."""
    gray = image if image.mode == "L" else image.convert("L")
    cut = paper_level(gray) - REST_FAINT_DELTA
    ink = gray.point(lambda v: 255 if v < cut else 0)
    tile = max(1, round(REST_TILE_PT * scale))
    densest = ink.reduce(tile).getextrema()[1]  # each pixel of the reduced image is one tile's mean
    return densest * tile * tile / 255 >= MIN_INK_PIXELS


def _ocr_page(doc: Document, index: int, page, width, height, page_class, signals, text: PageText, flags,
              rendered=None, *, camera_jpeg: bool = False, form_image=None, hybrid_image=None) -> None:
    """Read a page by OCR. ``rendered`` is (image, pixels per point) when the page is already an
    image (an image file, D4b); otherwise the PDF page is rendered."""
    hybrid = doc._hybrid
    gated = hybrid is not None and hybrid.confidence is not None
    if hybrid is not None and not gated and hybrid.eligible(doc, signals):
        image, scale = rendered if rendered is not None else _render(page)
        if rendered is None and scale * 72 < RENDER_DPI:
            flags = flags + ["render_dpi_reduced"]
        rendered = image, scale
        hosted_image = image if hybrid_image is None else hybrid_image
        if doc._hybrid.read(doc, index, hosted_image, width, height, page_class, signals, text.chars_total, flags):
            signals["ocr_hybrid_render_dpi"] = round(scale * 72, 1) if hybrid_image is None else signals["dpi"]
            return
    if page_class == "T5" and doc._handwriting is not None:
        from heocr.handwriting import read_page as read_handwriting

        image, scale = rendered if rendered is not None else _render(page)
        if rendered is None and scale * 72 < RENDER_DPI:
            flags = flags + ["render_dpi_reduced"]
        record = Page(index, width, height, page_class, "ocr", dict(signals),
                      {"chars_total": text.chars_total}, list(flags))
        if read_handwriting(doc, image, record, scale):
            return
        flags = record.flags
        rendered = image, scale
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
    # A skewed scan is turned straight first: ruling detection looks for straight runs, and at a
    # degree of skew a ruling leaves its pixel row before it is long enough to count (set C's heavily
    # skewed table scans had no table found at all). Every box this page records is turned back.
    # A page with ruled tables is turned from SKEW_MIN_DEG: its cells are read one by one and need
    # straight rows. Ordinary OCR turns running text and unruled tables only from
    # DESKEW_TEXT_MIN_DEG. The dropout repair below can corroborate a smaller turn.
    deskew = skew_angle(image)
    shallow_turn = None
    first_block, first_unread = len(doc.blocks), len(doc.unread)
    grids = tables_in(image, scale)
    if deskew:
        source = image if image.mode in ("L", "RGB") else image.convert("RGB")
        level = paper_level(source)
        turned = source.rotate(deskew, resample=_BICUBIC, fillcolor=level if source.mode == "L" else (level, level, level))
        turned_grids = tables_in(turned, scale)
        if turned_grids or (not grids and abs(deskew) >= DESKEW_TEXT_MIN_DEG):
            image, grids = turned, turned_grids
            if form_image is not None:
                form_image = form_image.rotate(deskew, resample=_BICUBIC, fillcolor=(level,) * 3)
            flags = flags + ["deskewed"]
            signals["deskew_degrees"] = deskew
        else:
            if not grids:
                shallow_turn = (turned, deskew)
            deskew = 0.0
    # Ruled tables in the image are read cell by cell; the rest of the page once, without them (D3e).
    rectified = rectify_grid(image, scale, tolerate_gaps=camera_jpeg) if not grids else None
    if doc._table_proposer is not None and rectified is None:
        # Learned proposals (D3g) read cell by cell like ruled grids; ruled grids keep first claim.
        grids, proposal_flags = _ocr_table_proposals(doc, index, image, scale, grids, camera_jpeg)
        flags = flags + proposal_flags
    rectified_read = None
    page_failed = False
    last_page = index + 1 >= min(doc.page_count or index + 1, doc._max_pages)
    dpi = round(scale * 72)
    probe = None
    probed = rectified is not None and routes.pending(doc)
    if probed:
        # The unmasked reading can choose the model and serve as fallback.
        try:
            probe = engine.recognize(image, dpi=dpi)
        except OcrUnavailable:
            # The current engine can still read isolated cells after a full-page failure.
            pass
    try:
        if probe is not None and routes.observe(doc, image, probe, dpi, last_page=last_page):
            engine = engine_for(doc)
            probe = None
        if rectified is not None:
            numbers = number_engine_for(doc) if doc.options.get("check_numbers", "suspect") else None
            rectified_read = _validated_rectified_tables(doc, index, engine, rectified, scale, numbers, image.size)
            if rectified_read is None:
                rectified = None
                flags = flags + ["ocr_grid_cell_fallback"]
            else:
                flags = flags + ["ocr_grid_rectified"]

        page_image = _without(image, scale, [t.bbox for t in grids], polygon=rectified.mask if rectified else ()) if grids or rectified else image
        # Bare paper outside a table can produce noise when Tesseract thresholds its texture.
        if (grids or rectified) and not _rest_has_ink(page_image, scale):
            result = OcrResult([])
            flags = flags + ["ocr_rest_blank"]
        else:
            result = probe if probe is not None and page_image is image else None
            if (page is not None and page_class == "T5" and doc._rtl and dpi == RENDER_DPI and engine.psm == 3
                    and not (grids or rectified or routes.pending(doc))):
                from heocr.ocr.scan_gaps import bridge_scan_stroke_gaps, has_prose_column

                # Keep a corroborated repair at the current angle. When tilt
                # hides its dropout bands, a shallow turn can expose them.
                trials = [(page_image, 0.0)]
                if shallow_turn is not None:
                    trials.append(shallow_turn)
                for trial, angle in trials:
                    bridged_image, bands = bridge_scan_stroke_gaps(trial)
                    if not bands:
                        continue
                    try:
                        candidate = engine.recognize(bridged_image, dpi=dpi)
                    except OcrUnavailable:
                        continue
                    if not has_prose_column(candidate.lines, trial.width):
                        continue
                    if angle:
                        image = page_image = trial
                        deskew = angle
                        flags = flags + ["deskewed"]
                        signals["deskew_degrees"] = angle
                    result = candidate
                    flags = flags + ["ocr_scan_stroke_gaps_bridged"]
                    signals["ocr_scan_dropout_bands"] = bands
                    break
            if result is None:
                result = engine.recognize(page_image, dpi=dpi)
        if not probed and routes.pending(doc) and routes.observe(doc, page_image, result, dpi, last_page=last_page):
            engine = engine_for(doc)
            result = engine.recognize(page_image, dpi=dpi)
    except OcrUnavailable as exc:
        doc.unread.append(Unread(scope="page", reason="ocr_failed", page=index, detail=str(exc)))
        flags = flags + ["ocr_failed"]
        if rectified_read is None:
            doc.pages.append(Page(index, width, height, page_class, "none", signals,
                                  {"chars_total": text.chars_total, "chars_read": 0, "status": "unread"}, flags))
            return
        # The isolated table is already read. Keep it even if OCR outside it failed.
        result = OcrResult([])
        page_failed = True
    historical = None
    settings = doc._ocr_settings or {}
    layout = settings.get("layout") == "v1"
    if layout and not (grids or rectified or page_failed or time_up(doc)):
        padded = ocr_layout.clip_guard(engine, page_image, dpi, result)
        if padded is not None:
            result = padded
            flags = flags + ["ocr_layout_clip_guard"]
    if (settings.get("old_print_model") is not None and not (grids or rectified or page_failed)
            and settings.get("capture_type") != "issuer-render"):
        from heocr.ocr.old_print import read_page as read_old_print

        historical = read_old_print(doc, page_image, dpi, result)
        if historical is not None:
            engine, result = historical
            signals["ocr_old_print"] = "phist_v2"
    first = len(doc.blocks)
    numbers = number_engine_for(doc) if doc.options.get("check_numbers", "suspect") else None
    number_words = None
    if numbers is not None and numbers is not engine:
        batch_numbers = settings.get("number_batch") or (page is None and settings.get("phone_geometry"))
        number_words = _reread_number_words(numbers, page_image, result, dpi,
                                           batch=bool(batch_numbers) and doc._deadline is None)
    page_numbers = None
    english_page = None
    if number_words is not None and PAGE_NUMBER_PASS:
        try:
            page_numbers, english_page = _page_number_pass(numbers, page_image, result, round(scale * 72))
        except OcrUnavailable:
            page_numbers = None
    # An unruled table is read by Tesseract column by column; its cells are given back in rows.
    ltr_changes = ocr_layout.normalize_ltr(result.lines) if layout and doc._rtl else 0
    unruled = table_rows(result.lines, scale, doc._rtl, layout=layout) if OCR_TABLE_ROWS else []
    taken = {id(line) for table in unruled for line in table.lines}
    if english_page is not None and numbers.lang == "eng":
        english, english_size = english_page
        body = (line for line in result.lines if id(line) not in taken)
        reread_latin_runs(page_image, round(scale * 72), body, english, english_size, lambda: latin_reviewer_for(doc))
    gray = page_image.convert("L")
    short, outside_share = short_for_ink(gray, result)
    recovered_lines = 0
    pixel_gap_lines = 0
    fragments_removed = 0
    if page is not None and page_class == "T5" and doc._rtl and not (unruled or grids or rectified) and not time_up(doc):
        supplemented = recover_scan_lines(engine, gray, result, scale, outside_share)
        if supplemented is not result:
            recovered_lines = len(supplemented.lines) - len(result.lines)
            result = supplemented
            short, outside_share = short_for_ink(gray, result)
            flags = flags + ["ocr_same_column_lines_recovered"]
        before_pixels = result
        stop = lambda: time_up(doc)
        recovered = recover_pixel_lines(engine, gray, result, scale, outside_share, stop=stop, short=short)
        cleaned = suppress_contained_fragments(engine, gray, recovered, scale, stop=stop)
        result = recover_narrow_lines(engine, gray, cleaned, scale, outside_share, stop=stop)
        fragments_removed = len(recovered.lines) - len(cleaned.lines)
        pixel_gap_lines = len(recovered.lines) - len(before_pixels.lines) + len(result.lines) - len(cleaned.lines)
        if result is not before_pixels:
            short, outside_share = short_for_ink(gray, result)
        if pixel_gap_lines:
            flags = flags + ["ocr_pixel_lines_recovered"]
        if fragments_removed:
            flags = flags + ["ocr_contained_fragments_removed"]
    photo_gap_lines = 0
    joined_camera_lines = 0
    if (page is None and page_class == "T5" and signals.get("image_format") == "jpeg" and doc._rtl
            and not (unruled or grids or rectified) and not time_up(doc)):
        supplemented = recover_photo_lines(engine, gray, result, dpi)
        if supplemented is not result:
            photo_gap_lines = len(supplemented.lines) - len(result.lines)
            result = supplemented
            short, outside_share = short_for_ink(gray, result)
            flags = flags + ["ocr_photo_gap_lines_recovered"]
        if camera_jpeg and short:
            # Bound the proposal analysis on large camera images. Crop reads still
            # use the original raster.
            narrow = recover_narrow_lines(engine, gray, result, scale, outside_share,
                                          stop=lambda: time_up(doc), preview_size=1150)
            if narrow is not result:
                pixel_gap_lines = len(narrow.lines) - len(result.lines)
                result = narrow
                short, outside_share = short_for_ink(gray, result)
                flags = flags + ["ocr_pixel_lines_recovered"]
        if camera_jpeg:
            joined = join_camera_line_fragments(engine, gray, result, scale, stop=lambda: time_up(doc))
            if joined is not result:
                joined_camera_lines = len(result.lines) - len(joined.lines)
                result = joined
                flags = flags + ["ocr_camera_line_fragments_joined"]
    if (page is None and settings.get("phone_geometry") and historical is None
            and not (page_failed or unruled or grids or rectified or time_up(doc))):
        from heocr.ocr.phone_geometry import read_lines as read_phone_lines

        if "phone_model" in settings:
            from heocr.ocr.phone_profile import line_engine

            corrected = read_phone_lines(engine, gray, result, line_engine=lambda: line_engine(doc))
        else:
            corrected = read_phone_lines(engine, gray, result)
        if corrected is not None:
            result = corrected
            short, outside_share = short_for_ink(gray, result)
            flags = flags + ["ocr_phone_geometry_rectified"]
            if profile := doc.options["ocr"].get("profile"):
                signals["ocr_phone_geometry_line_sha256"] = profile["line_model_sha256"]
    columns = None
    if layout and doc._rtl:
        ltr_changes += ocr_layout.normalize_ltr(line for line in result.lines if id(line) not in taken)
        if ltr_changes:
            signals["ocr_layout_ltr_runs"] = ltr_changes
        if not (unruled or grids or rectified):
            columns = ocr_layout.column_order(result.lines, page_image.width)
            if columns is not None:
                result = OcrResult(columns)
                flags = flags + ["ocr_layout_rtl_columns"]
    form_tables = [t.bbox for t in grids if "table_open_sides_inferred" in t.flags]
    margin_page = image.size if doc._rtl and not unruled and not (grids or rectified) else None
    record_lines = page is not None and page_class == "T5" and margin_page is not None
    line_records = _OcrLineRecords() if record_lines or gated else None
    read = _ocr_blocks(doc, index, engine, OcrResult([line for line in result.lines if id(line) not in taken]), scale,
                       form_tables=form_tables, margin_page=margin_page, line_records=line_records)
    if columns is None and _order_ocr_paragraphs(doc.blocks, first):
        flags = flags + ["reading_order_ocr_adjusted_unverified"]
    if columns is None and not (unruled or grids or rectified):
        if _order_ocr_bands(doc.blocks, first, image, scale):
            flags = flags + ["reading_order_ocr_bands_unverified"]
        elif record_lines and _order_ocr_gaps(doc.blocks, first, index, engine, scale, line_records):
            flags = flags + ["reading_order_ocr_disjoint_gaps_unverified"]
    if unruled:
        row_blocks, in_rows = _ocr_row_tables(doc, index, engine, unruled, scale)
        _place_tables(doc, first, row_blocks)
        read += in_rows
        flags = flags + ["unruled_tables"]
    if grids or rectified:
        if rectified_read is not None:
            t_blocks, in_cells = rectified_read
        else:
            form_probe = ()
            if (page is not None and page_class == "T5" and doc._rtl and not time_up(doc)
                    and any(needs_form_probe(image, grid, scale) for grid in grids)):
                try:
                    form_probe = engine.recognize(image, dpi=dpi).lines
                except OcrUnavailable:
                    pass
            table_image = image
            if (page is not None and page_class == "T5" and doc._rtl and dpi == RENDER_DPI and engine.psm == 3
                    and not (page_failed or routes.pending(doc) or time_up(doc))
                    and all("table_open_sides_inferred" not in grid.flags for grid in grids)):
                from heocr.ocr.scan_gaps import bridge_scan_stroke_gaps

                table_image, bands = bridge_scan_stroke_gaps(image)
                if bands:
                    flags = flags + ["ocr_scan_table_stroke_gaps_bridged"]
                    signals["ocr_scan_table_dropout_bands"] = bands
            t_blocks, in_cells = _ocr_tables(doc, index, engine, table_image, scale, grids,
                                            None if numbers is engine else numbers,
                                            **({"form_probe": form_probe} if form_probe else {}))
        for block in t_blocks:
            tidy(block)  # D3g: learned proposals only; ruled grids are never reshaped
        _place_tables(doc, first, t_blocks)
        read += in_cells
        flags = flags + ["ocr_tables"]
        if any("table_cells_ink_unread" in t.flags for t in t_blocks):
            flags = flags + ["table_cells_ink_unread"]
    if historical is None and settings.get("line_v6") is not None:
        from heocr.ocr.line_models import read_page_lines as route_page_lines

        read += route_page_lines(doc, doc.blocks[first:], page_image, scale, engine)
    if doc._crnn is not None:
        from heocr.ocr.crnn import read_page_lines

        read += read_page_lines(doc, doc.blocks[first:], page_image, scale)
    entries = []
    if form_image is not None and not page_failed and not time_up(doc):
        from heocr.pdf.formtext import recover_colored_form_entries

        tables = [[v * scale for v in block.bbox] for block in doc.blocks[first:] if block.kind == "table"]
        entries = recover_colored_form_entries(engine, form_image, gray, result, tables, dpi, lambda: time_up(doc))
        if entries:
            read += _ocr_blocks(doc, index, engine, OcrResult(entries), scale, flags=["ocr_form_entry"])
            flags = flags + ["ocr_form_entries_recovered"]
    if gated:
        read += hybrid.read_lines(doc, doc.blocks[first:], page_image, line_records, signals)
        if signals["ocr_hybrid_lines"]["changed"]:
            flags = flags + ["ocr_hybrid_unverified"]
    if page_class == "T4":
        flags = flags + ["text_layer_ignored"]
    elif page_class == "T3":
        flags = flags + ["text_layer_ignored_broken_mapping"]
    found = read > 0 or (doc._crnn is None and bool(result.lines))  # text read in table cells counts (D3e)
    if not found:
        doc.unread.append(Unread(scope="page", reason="ocr_found_no_text", page=index, detail=f"class {page_class}"))
    if short and found:
        flags = flags + ["ocr_short_for_ink"]
        doc.unread.append(
            Unread(scope="page", reason="ocr_short_for_ink", page=index, detail=f"{outside_share:.0%} of the ink lies outside every OCR line")
        )
    if deskew:
        center = (image.size[0] / 2 / scale, image.size[1] / 2 / scale)
        _unrotate_records(doc, first_block, first_unread, index, deskew, center)
    doc.pages.append(
        Page(index, width, height, page_class, "ocr", signals,
             {"chars_total": text.chars_total, "chars_read": 0, "chars_ocr": read,
              "ink_outside_ocr_share": outside_share,
              **({"ocr_form_entries_recovered": len(entries)} if entries else {}),
              **({"ocr_same_column_lines_recovered": recovered_lines} if recovered_lines else {}),
              **({"ocr_photo_gap_lines_recovered": photo_gap_lines} if photo_gap_lines else {}),
              **({"ocr_pixel_lines_recovered": pixel_gap_lines} if pixel_gap_lines else {}),
              **({"ocr_contained_fragments_removed": fragments_removed} if fragments_removed else {}),
              **({"ocr_camera_line_fragments_joined": joined_camera_lines} if joined_camera_lines else {}),
              **({"number_words_reread": number_words[0], "number_words_replaced": number_words[1]} if number_words else {}),
              **({"page_number_tokens": page_numbers[0], "page_number_words_replaced": page_numbers[1]} if page_numbers else {}),
              "status": "ocr" if found and not (short or page_failed) else "partial" if found else "unread"}, flags)
    )


# Unruled tables on OCR pages (``heocr.pdf.ocrrows``): off, the table stays as OCR text.
OCR_TABLE_ROWS = True


def _ocr_row_tables(doc: Document, index: int, engine, tables: list[TableRows], scale: float) -> tuple[list[Block], int]:
    """Table blocks for unruled tables found in an OCR result, their cells in rows. Returns the
    blocks and their characters."""
    to_pt = 1 / scale
    blocks: list[Block] = []
    read = 0
    for table in tables:
        cells = [[Cell(" ".join(piece.text for piece in cell)) for cell in row] for row in table.rows]
        read += sum(1 for row in cells for cell in row for ch in cell.text if not ch.isspace())
        replaced = sum(line.numbers_replaced for line in table.lines)
        readings = [r for line in table.lines for r in line.number_readings]
        blocks.append(
            Block(
                kind="table",
                cells=cells,
                page=index,
                bbox=[v * to_pt for v in table.bbox],
                source="ocr",
                tool=engine.version,
                direction="rtl" if doc._rtl else "ltr",
                ocr={**block_ocr(engine, table.lines), "dpi": round(scale * 72, 1), **({"number_words_replaced": replaced} if replaced else {}),
                     **({"duplicate_lines_dropped": table.duplicates} if table.duplicates else {})},
                checks={"number_readings": readings} if readings else None,
                flags=["unruled_table", "ocr_unverified"] + (["render_dpi_reduced"] if scale * 72 < RENDER_DPI else [])
                + (["numbers_from_second_reading"] if replaced else []),
            )
        )
    return blocks, read


def _without(image, scale: float, boxes, *, polygon=()) -> object:
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
    if polygon:
        draw.polygon(polygon, fill=fill)
    return out


def _validated_rectified_tables(doc: Document, index: int, engine, rectified, scale: float, numbers, page_size) -> tuple[list[Block], int] | None:
    """Stage isolated reads before committing a projective mask to the page."""
    before = len(doc.unread)
    factor = min(1, CELL_TEXT_PAGE_EDGE / max(page_size))
    blocks, chars = _ocr_tables(doc, index, engine, rectified.image, scale, [rectified.grid],
                                None if numbers is engine else numbers, text_scale=factor)
    unread = doc.unread[before:]
    rows = [row for block in blocks for row in block.cells]
    read_rows = [row for row in rows if any(cell.text.strip() for cell in row)]
    # An inked row may yield only ruling marks such as "|". Require a letter or digit in each read row.
    if (not read_rows
            or not all(any(ch.isalnum() for cell in row for ch in cell.text) for row in read_rows)
            or any("ocr_failed" in cell.flags for row in rows for cell in row)
            or any(entry.reason == "table_row_ink_unread" for entry in unread)):
        del doc.unread[before:]
        return None
    for block in blocks:
        block.bbox = rectified.source_box(block.bbox, scale)
    for entry in unread:
        if entry.bbox is not None:
            entry.bbox = rectified.source_box(entry.bbox, scale)
    return blocks, chars


def _ocr_tables(doc: Document, index: int, engine, image, scale: float, grids, numbers=None, offset=(0.0, 0.0), *, text_scale=None, form_probe=()) -> tuple[list[Block], int]:
    """Table blocks for ruled tables found in a page image, each inked cell OCR'd as its own region.

    With ``numbers`` (the number model), a cell whose reading holds digits is read a second time.
    ``offset`` (points) places an image that is a crop of the page (an image region, T6).
    ``text_scale`` enables tight, padded text crops inside rectified cells.
    """
    ox, oy = offset
    layout = (doc._ocr_settings or {}).get("layout") == "v1"
    dpi = round(scale * 72)
    blocks: list[Block] = []
    read = 0
    for grid in grids:
        rows: list[list[Cell]] = []
        lines_all = []
        unread_cells = 0
        psms: Counter = Counter()
        preserved = 0
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
                faint = False
                if not cell_has_ink(crop):
                    if not cell_has_faint_ink(crop):
                        out_row.append(Cell("", colspan=cell["colspan"], rowspan=cell["rowspan"]))
                        continue
                    # Ink too light for the ink test: read with its contrast stretched, and flagged so.
                    crop, faint = faint_cell_image(crop), True
                inked += 1
                native_crop = crop if text_scale is not None and text_scale < 1 else None
                if text_scale is not None:
                    crop = isolated_cell_text(crop, text_scale)
                psm = cell_psm(crop)
                result = None
                try:
                    result = engine.recognize(crop, dpi=dpi, psm=psm)
                    text = "\n".join(line.text for line in result.lines if line.text.strip())
                    cell_flags = []
                except OcrUnavailable:
                    text, cell_flags = "", ["ocr_failed"]
                if result is not None and not text and native_crop is not None and not time_up(doc):
                    # Reducing a rectified crop can erase weak strokes in an otherwise readable cell.
                    native_crop = isolated_cell_text(native_crop, 1)
                    native_psm = cell_psm(native_crop)
                    try:
                        native = engine.recognize(native_crop, dpi=dpi, psm=native_psm)
                    except OcrUnavailable:
                        pass
                    else:
                        native_text = "\n".join(line.text for line in native.lines if line.text.strip())
                        if ((native.mean_confidence or 0) >= REGION_MIN_CONFIDENCE
                                and any(ch.isalnum() for ch in native_text)):
                            crop, psm, result, text = native_crop, native_psm, native, native_text
                            cell_flags.append("ocr_cell_native_scale_retry")
                if layout and result is not None:
                    if not time_up(doc):
                        padded = ocr_layout.clip_guard(engine, crop, dpi, result, psm=psm)
                        if padded is not None:
                            result = padded
                            cell_flags.append("ocr_layout_clip_guard")
                    if doc._rtl and ocr_layout.normalize_ltr(result.lines):
                        cell_flags.append("ocr_layout_ltr_runs")
                    text = "\n".join(line.text for line in result.lines if line.text.strip())
                psms[psm] += 1
                if result is not None:
                    lines_all.extend(result.lines)
                if faint:
                    cell_flags.append("faint_ink_read")
                if not text.strip():
                    empty += 1
                    cell_flags.append("ocr_empty_with_ink")
                readings = None
                if numbers is not None and numbers is not engine and CELL_HALF_READING and _number_like_cell(text):
                    # Read at half resolution first, before the cell's own second reading decides.
                    half = _half_number_reading(numbers, crop, dpi)
                else:
                    half = None
                second = None
                first = {"model": engine.lang, "text": text, "confidence": result.mean_confidence if text.strip() else None}
                checked = bool(text.strip()) and numbers is not None and any(ch.isdigit() for ch in text)
                if checked:
                    text, readings, second = _second_number_reading(numbers, crop, dpi, psm, first, cell_flags, checks)
                if half is not None:
                    taken = _take_half_reading(half, text, second, numbers, crop, dpi)
                    if taken:
                        checks["read_by_number_model"] += 1
                        cell_flags[:] = [f for f in cell_flags if f not in ("number_disagreement", "ocr_empty_with_ink")] + ["number_read_at_half_resolution"]
                        readings = [r for r in (readings or [first, second]) if r is not None] + [half]
                        if not text.strip():
                            empty -= 1
                        text = half["text"]
                        second = None  # settled: the half-resolution reading is confirmed by its own evidence
                # A third reading that agrees with the cell's text confirms it.
                confirmed = half is not None and half["text"] == text.strip()
                if checked and second is not None and not confirmed:
                    if _unconfirmed(first, second):
                        # Two independent readings that disagree, neither confident: the cell's text
                        # is a guess, and the record says so (fixed rule, not tuned to a measure).
                        cell_flags.append("table_cell_unconfirmed")
                        readings = readings or [first, second]
                        x0, y0, x1, y1 = cell["box"]
                        doc.unread.append(Unread(scope="block", reason="table_cell_unconfirmed", page=index,
                                                 bbox=[x0 + ox, y0 + oy, x1 + ox, y1 + oy],
                                                 detail=f"row {len(rows)}, column {len(out_row)}: two readings differ and neither is confident"))
                if text.strip() and _GARBLED_CELL.search(text) and not any(f.startswith("number_read_") for f in cell_flags):
                    # Letters and marks no table cell holds (letters and commas for an amount, ₪ fused with
                    # letters): a misreading, flagged on the cell itself, whatever the other cells' readings.
                    cell_flags.append("table_cell_garbled")
                    x0, y0, x1, y1 = cell["box"]
                    doc.unread.append(Unread(scope="block", reason="table_cell_garbled", page=index,
                                             bbox=[x0 + ox, y0 + oy, x1 + ox, y1 + oy],
                                             detail=f"row {len(rows)}, column {len(out_row)}: OCR read letters and marks no cell holds"))
                if form_probe and text_scale is None and result is not None and readings is None:
                    augmented = preserve_form_cell_lines(engine, image, cell["box"], scale, result,
                                                         form_probe, lambda: time_up(doc))
                    if augmented is not result:
                        native_ids = {id(line) for line in result.lines}
                        additions = [line for line in augmented.lines if id(line) not in native_ids]
                        preserved += len(additions)
                        lines_all.extend(additions)
                        if not text.strip():
                            empty -= 1
                            cell_flags.remove("ocr_empty_with_ink")
                        text = "\n".join(line.text for line in augmented.lines if line.text.strip())
                read += sum(1 for ch in text if not ch.isspace())
                out_row.append(Cell(text, colspan=cell["colspan"], rowspan=cell["rowspan"], flags=cell_flags, readings=readings))
            unread_cells += empty
            if inked and empty == inked:
                boxes = [c["box"] for c in row if c["box"] is not None]
                bbox = [min(b[0] for b in boxes) + ox, min(b[1] for b in boxes) + oy, max(b[2] for b in boxes) + ox, max(b[3] for b in boxes) + oy]
                doc.unread.append(Unread(scope="page", reason="table_row_ink_unread", page=index, bbox=bbox, detail=f"{inked} inked cells, OCR read none"))
            rows.append(out_row)
        _flag_cells_out_of_column(doc, index, grid, rows, (ox, oy))
        flags = ([] if PROPOSED in grid.flags else ["ruled_table"]) + ["ocr_unverified", *grid.flags]
        if unread_cells:
            flags.append("table_cells_ink_unread")
        if preserved:
            flags.append("table_native_lines_preserved")
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
                ocr={**block_ocr(engine, lines_all), "psm": "per_cell", "cells_psm7": psms[7], "cells_psm6": psms[6], "dpi": round(scale * 72, 1),
                     **({"native_lines_preserved": preserved} if preserved else {})},
                checks=checks if checks["numbers"] or checks["read_by_number_model"] else None,
                flags=flags + (["render_dpi_reduced"] if scale * 72 < RENDER_DPI else []),
            )
        )
    return blocks, read


_HEBREW_WORD_IN = re.compile(r"[\u05d0-\u05ea]{3,}")


# A scanned table's number cells read once more at half resolution: at 300 dpi the number model reads a
# cell's short line of digits poorly (a date read as letters), at 150 it reads them well, as on a page.
# Taken only under the conditions of ``_take_half_reading``, it rights most such cells and leaves the
# rest as they were.
CELL_HALF_READING = True
# A misread cell: marks no table cell holds, a final letter inside a word, ₪ against a letter, digits
# fused with two letters or more, or a letter glued to a digit by number punctuation (letters and commas
# for an amount). Cells it matches are read wrong; cells read right do not match it.
_GARBLED_CELL = re.compile(r"[=\]\[<>*+^~\\]|[ךםןףץ][\u05d0-\u05ea]|₪[\u05d0-\u05ea]|[\u05d0-\u05ea]₪|\d[\u05d0-\u05ea]{2,}|[\u05d0-\u05ea]{2,}\d|[\u05d0-\u05ea][.,/]+\d")
CELL_HALF_MIN_CONFIDENCE = 85.0  # alone, without the other evidence below
_CELL_NUMBER_SHAPE = re.compile(r"^[\d.,/%₪\-]+$")


NUMBER_COLUMN_SHARE = 2 / 3  # a column whose body cells mostly hold digits is a column of numbers
NUMBER_COLUMN_MIN_CELLS = 3


def _flag_cells_out_of_column(doc: Document, index: int, grid, rows: list[list[Cell]], offset) -> None:
    """In a column of numbers (``NUMBER_COLUMN_SHARE`` of its read body cells hold a digit), a body cell
    read with no digit at all is a misreading: heb can read an amount as letters and marks. It is flagged
    ``table_cell_not_a_number`` with its own unread entry, whatever the other cells' readings. The first
    row (a header) is not checked. Compare by the grid's physical column: a cell spanning
    several columns has no single column type, and can shift the other cells' list positions."""
    ox, oy = offset
    columns: dict[int, list[tuple[int, int, Cell, list[float]]]] = defaultdict(list)
    for r, (row, grid_row) in enumerate(zip(rows[1:], grid.rows[1:]), 1):
        ordered = sorted(grid_row, key=lambda cell: -cell["col"] if doc._rtl else cell["col"])
        for c, (cell, geometry) in enumerate(zip(row, ordered)):
            if cell.colspan == 1 and cell.text.strip():
                columns[geometry["col"]].append((r, c, cell, geometry["box"] or grid.bbox))
    for body in columns.values():
        if len(body) < NUMBER_COLUMN_MIN_CELLS or sum(any(ch.isdigit() for ch in cell.text) for _, _, cell, _ in body) < NUMBER_COLUMN_SHARE * len(body):
            continue
        for r, c, cell, box in body:
            if any(ch.isdigit() for ch in cell.text) or "table_cell_garbled" in (cell.flags or []):
                continue
            cell.flags = list(cell.flags or []) + ["table_cell_not_a_number"]
            doc.unread.append(Unread(scope="block", reason="table_cell_not_a_number", page=index,
                                     bbox=[box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy],
                                     detail=f"row {r}, column {c}: no digit in a column of numbers"))


def _number_like_cell(text: str) -> bool:
    """A cell heb read with no Hebrew word and no Latin letter: digits misread as letters ("סזכ,./" for
    4,370), a lone letter ("ו" for a date), digits, or nothing at all though it has ink."""
    return not _HEBREW_WORD_IN.search(text) and not re.search(r"[A-Za-z]", text)


def _half_number_reading(numbers, crop, dpi: int) -> dict | None:
    """The number model's one-line reading of a cell at half resolution, when it is number-shaped."""
    from PIL import Image

    gray = crop.convert("L")
    w, h = gray.size
    half = gray.resize((max(1, w // 2), max(1, h // 2)), Image.LANCZOS)
    try:
        result = numbers.recognize(half, dpi=max(1, dpi // 2), psm=7)
    except OcrUnavailable:
        return None
    words = [w for line in result.lines for w in line.words if w.text.strip("|")]
    text = " ".join(w.text for w in words)
    if not words or not _CELL_NUMBER_SHAPE.match(text.replace(" ", "")):
        return None
    return {"model": numbers.lang, "text": text, "confidence": round(sum(w.confidence for w in words) / len(words), 2), "dpi": max(1, dpi // 2)}


def _take_half_reading(half: dict, text: str, second: dict | None, numbers, crop, dpi: int) -> bool:
    """Whether the half-resolution reading replaces the cell's text: it differs, and it agrees with the
    number model's full-resolution reading, or is a whole date or grouped amount, or is read with
    confidence at least ``CELL_HALF_MIN_CONFIDENCE``. Each of the three is evidence the new reading is
    right; without them, most changes would be wrong."""
    if half["text"] == text.strip():
        return False
    if second is None:
        try:
            result = numbers.recognize(crop, dpi=dpi, psm=7)
            full = " ".join(w.text for line in result.lines for w in line.words if w.text.strip("|"))
        except OcrUnavailable:
            full = ""
    else:
        full = " ".join(w for w in second["text"].split() if w.strip("|"))
    return full == half["text"] or bool(_WELL_FORMED.match(half["text"])) or (half["confidence"] or 0) >= CELL_HALF_MIN_CONFIDENCE
# A whole date (d.m.yyyy) or an amount in comma groups, nothing else in the cell.
_WELL_FORMED = re.compile(r"^(?:\d{1,2}[./]\d{1,2}[./](?:\d{2}|\d{4})|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)$")
WELL_FORMED_MIN_CONFIDENCE = 30.0


# line_fold_v1 (D-bench's scoring profile): NFC, signs our references do not write consistently, spaces.
_LINE_FOLD = str.maketrans({"־": "-", "׳": "'", "״": '"', "“": '"', "”": '"', "„": '"', "—": "–"})


def _line_fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).translate(_LINE_FOLD).split())


def _unconfirmed(first: dict, second: dict) -> bool:
    """A cell's two readings differ after line_fold_v1, and neither has confidence of at least
    ``numbers.MIN_CONFIDENCE`` (``table_cell_unconfirmed``)."""
    from heocr.pdf.numbers import MIN_CONFIDENCE

    if _line_fold(first["text"]) == _line_fold(second["text"]):
        return False
    return all((reading["confidence"] or 0.0) < MIN_CONFIDENCE for reading in (first, second))


def _second_number_reading(numbers, crop, dpi: int, psm: int, first: dict, cell_flags: list[str], checks: dict) -> tuple[str, list | None, dict | None]:
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
        return text, None, None
    # A cell's own rulings come back as "|" tokens, at confidences that pull the reading's mean down
    # ("222,720 |" at 95.9 and 73.7): they are no part of the cell's text.
    kept = [[w for w in line.words if w.text.strip("|")] for line in result.lines]
    second = "\n".join(" ".join(w.text for w in words) for words in kept if words)
    values = [w.confidence for words in kept for w in words if w.confidence >= 0]
    confidence = round(sum(values) / len(values), 2) if values else None
    reading = {"model": numbers.lang, "text": second, "confidence": confidence}
    # A numeric cell: the number model reads no letter, and heb read no Hebrew word (a misreading
    # of digits as letters, "סז5,.?" for 4,370, is no word).
    if not _HEBREW_WORD_IN.search(text) and not any("a" <= ch.lower() <= "z" for ch in text) and second and not any(ch.isalpha() for ch in second):
        if second.split() == text.split():
            checks["numbers"] += len(significant(text))
            checks["agreed"] += len(significant(text))
            return text, None, reading
        well_formed = _WELL_FORMED.match(second) and not _WELL_FORMED.match(text)
        if confidence is not None and (confidence >= MIN_CONFIDENCE or (well_formed and confidence >= WELL_FORMED_MIN_CONFIDENCE)):
            # Both readings are OCR: this chooses between two readings, it overwrites no layer
            # text. Both are kept in the cell; the block stays source "ocr", unverified. A reading
            # that is a whole date or a grouped amount, against one that is neither ("14.09.2023"
            # against "₪11.22"), is taken at a lower confidence: its form is evidence of its own.
            checks["read_by_number_model"] += 1
            cell_flags.append("number_read_by_number_model")
            return second, [first, reading], reading
    checked, agreed, disagreed, _ = compare(text, second, confidence)
    checks["numbers"] += checked
    checks["agreed"] += agreed
    checks["disagreed"] += disagreed
    if disagreed:
        cell_flags.append("number_disagreement")
    return text, None, reading


def _layer_line_boxes(boxes) -> list[list[float]]:
    """A text layer's character boxes merged into line boxes (points): characters whose vertical
    centres lie within half a height of each other and that sit at most three heights apart."""
    lines: list[list[float]] = []
    for x0, y0, x1, y1 in sorted(boxes, key=lambda b: ((b[1] + b[3]) / 2, b[0])):
        h = max(y1 - y0, 1e-6)
        cy = (y0 + y1) / 2
        for line in reversed(lines[-8:]):
            lh = line[3] - line[1]
            if abs((line[1] + line[3]) / 2 - cy) <= 0.5 * max(h, lh) and x0 - line[2] <= 3 * h and line[0] - x1 <= 3 * h:
                line[0], line[1], line[2], line[3] = min(line[0], x0), min(line[1], y0), max(line[2], x1), max(line[3], y1)
                break
        else:
            lines.append([x0, y0, x1, y1])
    return lines


def _repeat_region(doc: Document, index: int, box, record: dict, digest: str) -> int:
    """Copies of a repeated image's first reading, placed in this occurrence's box (``box``), flagged
    ``repeated_image_text`` and pointing to the first one. Returns the characters emitted."""
    fx0, fy0, fx1, fy1 = record["box"]
    sx = (box[2] - box[0]) / max(fx1 - fx0, 1e-6)
    sy = (box[3] - box[1]) / max(fy1 - fy0, 1e-6)

    def moved(b):
        return [box[0] + (b[0] - fx0) * sx, box[1] + (b[1] - fy0) * sy, box[0] + (b[2] - fx0) * sx, box[1] + (b[3] - fy0) * sy]

    chars = 0
    for original in record["blocks"]:
        block = copy.deepcopy(original)
        block.page = index
        if block.bbox:
            block.bbox = moved(block.bbox)
        for line in block.lines or []:
            line.bbox = moved(line.bbox)
        block.flags = [f for f in block.flags if f != "repeated_image_text"] + ["repeated_image_text"]
        block.checks = {**(block.checks or {}), "repeated_image": {"first_page": record["page"], "image": digest}}
        chars += sum(1 for ch in block.text if not ch.isspace())
        doc.blocks.append(block)
    return chars


def _ocr_regions(
    doc: Document, index: int, page, width: float, height: float, boxes: list[list[float]], drawn_boxes, ocr_wanted: bool = True,
    layer_is_ocr: bool = False,
    digests: list[str] | None = None,
) -> dict:
    """Region OCR for the image regions of a page read from its text layer. Returns coverage counts.

    The layer's own characters are masked out of each region first, so text already read from
    the layer (labels over a diagram, text on a background image) is not read again by OCR.
    Regions under REGION_MIN_PAGE_SHARE of the page (logos, icons) are counted, not recognized.
    On a page whose layer is an OCR layer that was kept (``layer_is_ocr``), an image covering the
    page (``PAGE_IMAGE``) is that layer's own scan: it is counted as needing no OCR. Masking the
    layer out of a scan leaves residue that OCR reads as noise at low confidence, which was
    reported as unread although the layer holds the scan's text.
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
    digest_of = {id(b): d for b, d in zip(boxes, digests or [])}
    sized = [b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) >= REGION_MIN_PAGE_SHARE * width * height]
    if ocr_wanted:
        large = sized
    else:
        # Images under a T6 share of the page are mostly logos and icons, counted and not read. A
        # region that holds lines of text is read all the same (a table drawn as a small image was
        # skipped and lost with no flag): the layer's own characters are masked out first, and the
        # rest must look like text rows (text_rows).
        large = []
        if sized:
            image, scale = _render(page, frame_of(page))
            for box in sized:
                crop = [int(box[0] * scale), int(box[1] * scale), int(box[2] * scale + 0.999), int(box[3] * scale + 0.999)]
                if crop[2] - crop[0] < MIN_REGION_PX or crop[3] - crop[1] < MIN_REGION_PX:
                    continue
                inside = [d for d in drawn_boxes if d[0] < box[2] and d[2] > box[0] and d[1] < box[3] and d[3] > box[1]]
                region, _ = mask_layer_text(image.crop(crop), (crop[0] / scale, crop[1] / scale), scale, inside)
                if text_rows(region):
                    large.append(box)
            if large:
                counts["image_regions_text_below_figure_share"] = len(large)
    counts["image_regions_small"] = len(boxes) - len(large)
    if layer_is_ocr:
        scans = [b for b in large if (b[2] - b[0]) * (b[3] - b[1]) >= PAGE_IMAGE * width * height]
        counts["image_regions_no_ocr_needed"] += len(scans)
        counts["image_regions_layer_scan"] = len(scans)
        large = [b for b in large if b not in scans]
        if scans:
            # The skip assumes the layer covers its scan. A sandwich layer is often partial (the
            # OCR software skipped a table, a stamp, a faint paragraph): the scan's ink outside the
            # layer's characters is measured as on an OCR page, and reported when OCR would be.
            image, scale = _render(page, frame_of(page))
            grey = image.convert("L")
            lines = _layer_line_boxes(drawn_boxes)
            heights = sorted(b[3] - b[1] for b in lines) or [0.0]
            pad = max(2, int(LAYER_SCAN_PAD * heights[len(heights) // 2] * scale))
            # Rulings are ink with no text on them: they count as covered.
            horizontal, vertical = image_rulings(image, scale)
            covered = lines + [[r.start, r.pos - RULE_PAD_PT, r.end, r.pos + RULE_PAD_PT] for r in horizontal]
            covered += [[r.pos - RULE_PAD_PT, r.start, r.pos + RULE_PAD_PT, r.end] for r in vertical]
            total, outside = dense_ink_outside(grey, [[b[0] * scale, b[1] * scale, b[2] * scale, b[3] * scale] for b in covered], pad=pad,
                                               tile_px=max(8, int(LAYER_SCAN_TILE_PT * scale)), density=LAYER_SCAN_DENSITY)
            share = round(outside / total, 4) if total else 0.0
            counts["layer_scan_ink_outside_share"] = share
            if outside >= SHORT_INK_MIN_PX and share >= LAYER_SCAN_SHORT_SHARE:
                counts["layer_scan_short_for_ink"] = 1
                doc.unread.append(Unread(scope="page", reason="ocr_short_for_ink", page=index, bbox=scans[0],
                                         detail=f"{share:.0%} of the scan's ink lies outside every layer character"))
    # The same image drawn again in the document (a letterhead, a logo, a stamp on every page) is read
    # once, where it first appears. Each later occurrence gets a copy of that reading, placed in its own
    # box and flagged repeated_image_text, with no OCR: every page keeps its text, as running headers do.
    # A first reading that was empty or unsure repeats as counts, never as new unread entries.
    repeated = [b for b in large if digest_of.get(id(b)) and digest_of[id(b)] in doc._regions_read]
    if repeated:
        counts["image_regions_repeated"] = len(repeated)
        counts["image_regions_no_ocr_needed"] += len(repeated)
        for box in repeated:
            copied = _repeat_region(doc, index, box, doc._regions_read[digest_of[id(box)]], digest_of[id(box)])
            counts["chars_repeated_image"] = counts.get("chars_repeated_image", 0) + copied
        large = [b for b in large if not any(b is r for r in repeated)]
    for box in large:
        if digest_of.get(id(box)):
            doc._regions_read.setdefault(digest_of[id(box)], {"page": index, "box": box, "blocks": []})
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
    image, scale = _render(page, frame_of(page))

    def read_region(box) -> None:
        crop = [int(box[0] * scale), int(box[1] * scale), int(box[2] * scale + 0.999), int(box[3] * scale + 0.999)]
        if crop[2] - crop[0] < MIN_REGION_PX or crop[3] - crop[1] < MIN_REGION_PX:
            unread("image_region_too_small", box)
            return
        inside = [b for b in drawn_boxes if b[0] < box[2] and b[2] > box[0] and b[1] < box[3] and b[3] > box[1]]
        region, masked = mask_layer_text(image.crop(crop), (crop[0] / scale, crop[1] / scale), scale, inside)
        if not has_ink(region):
            # Not a loss: the layer holds this region's text, or it holds none. Counted in coverage
            # (within image_regions_no_ocr_needed), never as unread: unread is what was not read.
            key = "image_regions_text_in_layer" if masked else "image_regions_blank"
            counts[key] = counts.get(key, 0) + 1
            counts["image_regions_no_ocr_needed"] += 1
            return
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
                return
        try:
            result = engine.recognize(region, dpi=round(scale * 72))
        except OcrUnavailable as exc:
            unread("ocr_failed", box, str(exc))
            return
        if not result.lines:
            if tables:
                counts["image_regions_ocr"] += 1  # its tables were read; the rest held no text
                return
            unread("image_region_no_text_found", box)
            return
        confidence = result.mean_confidence
        if confidence is not None and confidence < REGION_MIN_CONFIDENCE:
            # Logos and drawings yield stray marks at low confidence; they are reported, not emitted.
            unread("image_region_ocr_low_confidence", box, f"mean confidence {confidence}")
            return
        # Region text is placed among the page's text-layer blocks by its top edge (_place_region); where it
        # cannot be, it follows them, flagged.
        flags = ("from_image_region", "reading_order_region_appended") + (("layer_text_masked",) if masked else ())
        short, share = short_for_ink(region, result)
        if short:
            # In a region, ink beyond the text is often the drawing itself: flagged on the block, for review.
            flags += ("ocr_short_for_ink",)
        first = len(doc.blocks)
        numbers = number_engine_for(doc) if doc.options.get("check_numbers", "suspect") else None
        if numbers is not None and numbers is not engine:
            reread, replaced = _reread_number_words(numbers, region, result, round(scale * 72))
            if reread:
                counts["number_words_reread"] = counts.get("number_words_reread", 0) + reread
                counts["number_words_replaced"] = counts.get("number_words_replaced", 0) + replaced
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

    for box in large:
        mark = len(doc.blocks)
        read_region(box)
        record = doc._regions_read.get(digest_of.get(id(box), ""))
        if record is not None and record["box"] is box:
            record["blocks"] = [copy.deepcopy(b) for b in doc.blocks[mark:]]
        if _place_region(doc, index, mark, box):
            counts["image_regions_placed"] = counts.get("image_regions_placed", 0) + 1
    return counts


def _place_region(doc: Document, index: int, mark: int, box) -> bool:
    """Move a region's blocks (``doc.blocks[mark:]``) from the page's end to its place in the reading
    order, by the box its text covers (the union of those blocks; the image's box only when they have
    none: a full-slide background image would put a bottom logo strip's text at the page top). They go
    before the first of the page's earlier blocks in their column (overlapping them across) whose top
    is below the text's top, and before any blocks of the page just before that one which start at or
    below the text's bottom; else after the last of them, and past any later block of the page whose
    bottom is above the text's top. Placed blocks are flagged ``reading_order_region_placed`` instead
    of ``reading_order_region_appended``. Text beside no block of the page (a figure in an empty
    column), or below all of them, stays at the end. Returns whether it moved."""
    new = doc.blocks[mark:]
    if not new:
        return False
    boxes = [b.bbox for b in new if b.bbox is not None]
    if boxes:
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    column = [i for i in range(mark) if doc.blocks[i].page == index and doc.blocks[i].bbox is not None
              and min(doc.blocks[i].bbox[2], box[2]) > max(doc.blocks[i].bbox[0], box[0])]
    if not column:
        return False
    at = next((i for i in column if doc.blocks[i].bbox[1] > box[1]), None)
    if at is not None:
        # The text's column can start below an earlier block of another column: step back over the
        # page's blocks before it that start at or below the text's bottom (a top-of-page region).
        while at > 0 and doc.blocks[at - 1].page == index and doc.blocks[at - 1].bbox is not None and doc.blocks[at - 1].bbox[1] >= box[3]:
            at -= 1
    if at is None:
        at = column[-1] + 1
        while at < mark and doc.blocks[at].page == index and doc.blocks[at].bbox is not None and doc.blocks[at].bbox[3] <= box[1]:
            at += 1
    if at >= mark:
        return False
    del doc.blocks[mark:]
    doc.blocks[at:at] = new
    for block in new:
        block.flags = [("reading_order_region_placed" if f == "reading_order_region_appended" else f) for f in block.flags]
    return True


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
