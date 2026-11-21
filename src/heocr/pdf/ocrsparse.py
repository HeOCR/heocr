"""Recover a skipped line alongside already read prose on a ruled scan."""

from __future__ import annotations

from dataclasses import replace
from difflib import SequenceMatcher
from math import ceil
import re
from statistics import mean, median

from heocr.ocr.regions import paper_level
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable
from heocr.pdf.ocrtables import image_rulings

SPARSE_PSM = 11
LINE_PSM = 7
MAX_LINE_CHECKS = 6
MIN_LINE_CONFIDENCE = 60
MIN_LINE_AGREEMENT = 0.90
MIN_UNCOVERED_INK = 0.35
_HEBREW_LETTER = re.compile(r"[\u05d0-\u05ea]")


def _overlap(a, b, axis):
    return max(0, min(a[axis + 2], b[axis + 2]) - max(a[axis], b[axis]))


def _overlaps_line(box, other):
    area = (box[2] - box[0]) * (box[3] - box[1])
    return _overlap(box, other, 0) * _overlap(box, other, 1) >= 0.10 * area


def _reading_slot(box, original, width, height, *, edge_share=.05):
    """Locate the line in the existing stream without disturbing the native order."""
    x0, y0, x1, y1 = box
    if x1 - x0 < 0.15 * width or y1 <= y0:
        return None
    above = []
    below = []
    for index, native in enumerate(original):
        left, top, right, bottom = native.bbox
        overlap = _overlap(box, native.bbox, 0)
        if _overlaps_line(box, native.bbox):
            return None
        if (len(native.words) < 2 or overlap < 0.70 * min(x1-x0, right-left)
                or abs(right-x1) > edge_share * width):
            continue
        if 0 <= y0 - bottom <= 4 * height:
            above.append((y0 - bottom, index))
        if 0 <= top - y1 <= 4 * height:
            below.append((top - y1, index))
    before = min(above)[1] if above else -1
    after = min(below)[1] if below else len(original)
    if before >= after:
        return None
    if below:
        return after, original[after].paragraph
    if above:
        return before + 1, original[before].paragraph
    return None


def _matches_crop(engine, gray, text, bbox, dpi, paper):
    from PIL import ImageOps

    x0, y0, x1, y1 = bbox
    w, h = gray.size
    crop = gray.crop((max(0, int(x0)-3), max(0, int(y0)-3), min(w, ceil(x1)+3), min(h, ceil(y1)+3)))
    crop = ImageOps.expand(crop, border=10, fill=paper)
    checked = engine.recognize(crop, dpi=dpi, psm=LINE_PSM)
    if len(checked.lines) != 1:
        return False
    reread = " ".join(checked.lines[0].text.split())
    text = " ".join(text.split())
    return ([ch for ch in text if ch.isdecimal()] == [ch for ch in reread if ch.isdecimal()]
            and SequenceMatcher(None, text, reread, autojunk=False).ratio() >= MIN_LINE_AGREEMENT)


def recover_scan_lines(engine, gray, result, scale, outside_share):
    """Keep native lines and insert only independently corroborated same-column readings."""
    native = result.lines
    if engine.psm != 3 or len(native) < 2 or outside_share < MIN_UNCOVERED_INK:
        return result
    w, h = gray.size
    horizontal, vertical = image_rulings(gray, scale)
    if not (any((r.end-r.start)*scale >= 0.15*w for r in horizontal)
            or any((r.end-r.start)*scale >= 0.08*h for r in vertical)):
        return result
    dpi = round(scale * 72)
    try:
        sparse = engine.recognize(gray, dpi=dpi, psm=SPARSE_PSM)
    except OcrUnavailable:
        return result

    height = median(line.bbox[3]-line.bbox[1] for line in native)
    proposals = []
    for line in sparse.lines:
        if len(line.words) < 3 or len(_HEBREW_LETTER.findall(line.text)) < 6:
            continue
        confidence = mean(word.confidence for word in line.words)
        if confidence < MIN_LINE_CONFIDENCE:
            continue
        slot = _reading_slot(line.bbox, native, w, height)
        if slot is not None:
            proposals.append((-confidence, *slot, line))
    if not proposals:
        return result

    paper = paper_level(gray)
    insertions: dict[int, list[OcrLine]] = {}
    accepted = []
    for _, index, paragraph, line in sorted(proposals, key=lambda p: p[0])[:MAX_LINE_CHECKS]:
        if any(_overlaps_line(line.bbox, other.bbox) or _overlaps_line(other.bbox, line.bbox) for other in accepted):
            continue
        try:
            confirmed = _matches_crop(engine, gray, line.text, line.bbox, dpi, paper)
        except OcrUnavailable:
            continue
        if confirmed:
            accepted.append(line)
            insertions.setdefault(index, []).append(replace(line, paragraph=paragraph))
    if not insertions:
        return result

    lines = []
    for index in range(len(native) + 1):
        lines.extend(sorted(insertions.get(index, ()), key=lambda line: line.bbox[1]))
        if index < len(native):
            lines.append(native[index])
    return OcrResult(lines)
