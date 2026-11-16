"""Page signals and the text-layer classes T1–T6 (heocr-engine proposal ``document-pipeline-package.md`` §3.3).

Every threshold is a named constant with its reason. They are provisional until D-bench
measures them (``planning/milestones/d1-foundations.md``). The classes are tried in a fixed
order, and the first one that applies wins.
"""

from __future__ import annotations

import ctypes
import re
import unicodedata
from dataclasses import dataclass, field

from heocr.pdf.lines import WORD_GAP_EM, BuiltLine
from heocr.textflags import is_private_use

# Fewer drawn characters than this is no usable text layer, when images or paths are present.
MIN_TEXT_CHARS = 5
# Image coverage at which an image is the page (a scan), and below which images are decoration.
PAGE_IMAGE = 0.85
FIGURE_IMAGE = 0.10
# This many vector paths with no text suggests text drawn as outlines.
VECTOR_TEXT_PATHS = 100
# Hebrew words (two letters or more) starting with a final letter: none in logical text; about a third
# of words end with one, so a reversed layer shows many.
FINAL_START_RATE = 0.15
MIN_HEBREW_WORDS = 5
# Broken mapping: tendex counts U+FFFD above max(2, 0.5%) as unmapped.
BROKEN_MIN = 3
BROKEN_SHARE = 0.005
# An OCR layer (invisible text over a page image) is foreign OCR; below this Hebrew share it is garbage.
INVISIBLE_SHARE = 0.5
OCR_HEBREW_SHARE = 0.3
# A two-page spread is twice a standard portrait page at the same height (the volunteer's rule).
# Aspect alone cannot tell: A4 landscape has the same aspect as two A4 pages side by side.
# A5 is left out on purpose: two A5 pages are exactly A4 landscape, which is common (slides).
STANDARD_PORTRAIT = ((595.0, 842.0), (612.0, 792.0), (612.0, 1008.0), (842.0, 1191.0))
SPREAD_TOLERANCE = 0.02

_FINALS = frozenset("ךםןףץ")
_MATH_SIGNS = frozenset("\u00d7\u00f7")  # × and ÷ are Latin-1 but not letters
_HEBREW_LETTER = re.compile(r"[א-ת]")
_WORD = re.compile(r"[א-ת֑-ׇ׳״\"']+")


@dataclass
class ObjectStats:
    images: int = 0
    image_coverage: float = 0.0
    paths: int = 0
    text_objects: int = 0
    stream_text: str = ""
    image_boxes: list[list[float]] = field(default_factory=list)  # top-left origin, points


def _union_area(rects: list[tuple[float, float, float, float]]) -> float:
    """Exact area of a union of axis-aligned rectangles (coordinate compression)."""
    if not rects:
        return 0.0
    xs = sorted({x for r in rects for x in (r[0], r[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((r[1], r[3]) for r in rects if r[0] <= left and r[2] >= right)
        covered, current_start, current_end = 0.0, None, None
        for start, end in spans:
            if current_end is None or start > current_end:
                if current_end is not None:
                    covered += current_end - current_start
                current_start, current_end = start, end
            else:
                current_end = max(current_end, end)
        if current_end is not None:
            covered += current_end - current_start
        area += covered * (right - left)
    return area


def object_stats(page, max_objects: int = 20000) -> ObjectStats:
    """Images, paths and text objects of a page, with the text objects' text in content-stream order."""
    import pypdfium2.raw as raw

    stats = ObjectStats()
    width, height = page.get_size()
    rects = []
    textpage = page.get_textpage()
    pieces: list[str] = []
    previous_box = None
    try:
        for count, obj in enumerate(page.get_objects(max_depth=8)):
            if count >= max_objects:
                break
            kind = obj.type
            if kind == raw.FPDF_PAGEOBJ_IMAGE:
                stats.images += 1
                left, bottom, right, top = obj.get_bounds()
                left, right = max(0.0, left), min(width, right)
                bottom, top = max(0.0, bottom), min(height, top)
                if right > left and top > bottom and len(rects) < 500:
                    rects.append((left, bottom, right, top))
            elif kind == raw.FPDF_PAGEOBJ_PATH:
                stats.paths += 1
            elif kind == raw.FPDF_PAGEOBJ_TEXT:
                stats.text_objects += 1
                size = raw.FPDFTextObj_GetText(obj.raw, textpage, None, 0)
                if size <= 2:
                    continue
                buffer = ctypes.create_string_buffer(size)
                raw.FPDFTextObj_GetText(obj.raw, textpage, ctypes.cast(buffer, ctypes.POINTER(raw.FPDF_WCHAR)), size)
                text = buffer.raw[: size - 2].decode("utf-16-le", "replace")
                # pdfium attaches its own inserted spaces to object text; keep only drawn space glyphs.
                text = " " if text.isspace() else text.strip()
                box = obj.get_bounds()
                if previous_box is not None:
                    font_gap = WORD_GAP_EM * max(box[3] - box[1], 1.0)
                    same_line = abs((box[1] + box[3]) / 2 - (previous_box[1] + previous_box[3]) / 2) < (box[3] - box[1]) / 2
                    near = min(abs(box[0] - previous_box[2]), abs(previous_box[0] - box[2])) <= font_gap
                    if not (same_line and near):
                        pieces.append(" ")
                pieces.append(text)
                previous_box = box
    finally:
        textpage.close()
    stats.image_coverage = _union_area(rects) / (width * height) if width * height > 0 else 0.0
    stats.image_boxes = [[left, height - top, right, height - bottom] for left, bottom, right, top in rects]
    stats.stream_text = "".join(pieces)
    return stats


def _final_start_rate(text: str) -> tuple[float, int]:
    words = [w for w in _WORD.findall(text) if len(_HEBREW_LETTER.findall(w)) >= 2]
    if not words:
        return 0.0, 0
    starts = sum(1 for w in words if _HEBREW_LETTER.search(w).group(0) in _FINALS)
    return starts / len(words), len(words)


def text_signals(chars: str, lines: list[BuiltLine], invisible: int, total: int, symbol_pua: frozenset[str] | set[str] = frozenset()) -> dict:
    """Text-layer signals. ``symbol_pua``: private-use codes the page draws only in symbol fonts."""
    hebrew = len(_HEBREW_LETTER.findall(chars))
    letters = sum(1 for ch in chars if unicodedata.category(ch).startswith("L"))
    latin = sum(1 for ch in chars if "LATIN" in unicodedata.name(ch, "") and unicodedata.category(ch).startswith("L"))
    built = "\n".join(line.text for line in lines)
    rate, words = _final_start_rate(built)
    latin1_adjacent = 0
    for word in re.findall(r"\S+", built):
        if _HEBREW_LETTER.search(word):
            latin1_adjacent += sum(1 for ch in word if 0xC0 <= ord(ch) <= 0xFF and ch not in _MATH_SIGNS)
    # Private-use codes inside Hebrew words are broken letters (the volunteer's rule); symbol-font
    # bullets at line start are not.
    pua_hebrew = 0
    for line in built.split("\n"):
        for i, ch in enumerate(line):
            if is_private_use(ch) and ch not in symbol_pua:
                neighbours = line[max(i - 1, 0) : i] + line[i + 1 : i + 2]
                pua_hebrew += any(_HEBREW_LETTER.match(n) for n in neighbours)
    return {
        "chars": total,
        "hebrew_letters": hebrew,
        "latin_letters": latin,
        "hebrew_share": round(hebrew / letters, 4) if letters else 0.0,
        "hebrew_words": words,
        "final_start_rate": round(rate, 4),
        "replacement_chars": chars.count("�"),
        "private_use_chars": sum(1 for ch in chars if is_private_use(ch)),
        "private_use_next_to_hebrew": pua_hebrew,
        "latin1_next_to_hebrew": latin1_adjacent,
        "invisible_share": round(invisible / total, 4) if total else 0.0,
    }


def is_spread(width: float, height: float) -> bool:
    for w, h in STANDARD_PORTRAIT:
        if abs(height - h) <= SPREAD_TOLERANCE * h and abs(width - 2 * w) <= SPREAD_TOLERANCE * 2 * w:
            return True
    return False


def classify(signals: dict) -> tuple[str, list[str]]:
    """The page's class and the flags that explain it."""
    flags: list[str] = []
    chars = signals["chars"]
    coverage = signals["image_coverage"]
    paths = signals["paths"]
    if is_spread(signals.get("width", 0.0), signals.get("height", 0.0)):
        flags.append("possible_spread")
    if chars == 0:
        if signals["images"] and coverage > 0:
            return "T5", flags
        if paths >= VECTOR_TEXT_PATHS:
            return "T5", flags + ["text_may_be_vector_paths"]
        return "empty", flags
    if chars < MIN_TEXT_CHARS and (coverage >= PAGE_IMAGE or paths >= VECTOR_TEXT_PATHS):
        return "T5", flags + ["text_layer_too_small"]
    reversed_words = signals["hebrew_words"] >= MIN_HEBREW_WORDS and signals["final_start_rate"] >= FINAL_START_RATE
    ocr_layer = signals["invisible_share"] >= INVISIBLE_SHARE and coverage >= PAGE_IMAGE
    if ocr_layer:
        if signals["hebrew_share"] < OCR_HEBREW_SHARE or reversed_words:
            return "T4", flags + ["foreign_ocr_layer"]
        flags.append("text_layer_is_ocr")
    broken = signals["replacement_chars"] + signals["private_use_next_to_hebrew"] + signals["latin1_next_to_hebrew"]
    if broken >= BROKEN_MIN and broken >= BROKEN_SHARE * chars:
        return "T3", flags + ["broken_font_mapping"]
    stream_reversed = (
        signals["stream_hebrew_words"] >= MIN_HEBREW_WORDS and signals["stream_final_start_rate"] >= FINAL_START_RATE
    )
    if reversed_words:
        # Words read backwards even after our line building: the page itself draws them reversed.
        # Geometry cannot fix that, so the page is classified T2 but not read (D2a).
        return "T2", flags + ["text_layer_visual_order", "reversed_after_line_building"]
    if stream_reversed:
        # The content stream is in visual order; the geometric line building reads it in logical order.
        return "T2", flags + ["text_layer_visual_order"]
    if not ocr_layer and coverage >= FIGURE_IMAGE:
        return "T6", flags + ["images_may_hold_text"]
    return "T1", flags


def stream_signals(stream_text: str) -> dict:
    rate, words = _final_start_rate(stream_text)
    return {"stream_final_start_rate": round(rate, 4), "stream_hebrew_words": words}
