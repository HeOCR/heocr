"""Recover spatially separate blue form entries without rewriting native OCR."""

from __future__ import annotations

from dataclasses import replace
import re
from statistics import mean

from PIL import ImageChops, ImageOps

from heocr.ocr.regions import INK_LEVEL
from heocr.ocr.tesseract import OcrUnavailable
from heocr.pdf.ocrsparse import _overlap

BLUE_MARGIN = 16
MAX_CROPS = 24
SPARSE_PSM = 11
ENTRY = re.compile(r"[א-ת]{2,}|\d{2}")


def _ink_mask(image, cutoff):
    red, green, blue = image.split()
    excess = ImageChops.darker(ImageChops.subtract(blue, red), ImageChops.subtract(blue, green))
    return excess.point(lambda value: 0 if value >= cutoff else 255)


def _confident(line):
    return (line.words and ENTRY.search(line.text)
            and min(word.confidence for word in line.words) >= 65
            and mean(word.confidence for word in line.words) >= 80)


def _covered(box, other):
    return _overlap(box, other, 0) * _overlap(box, other, 1) >= .4 * (box[2] - box[0]) * (box[3] - box[1])


def recover_colored_form_entries(engine, rgb, gray, native, tables, dpi, stop):
    """Find and reread blue-only text in areas without a native word or table."""
    if engine.psm != 3 or stop():
        return []
    mask = _ink_mask(rgb, BLUE_MARGIN)
    pixels = rgb.width * rgb.height
    ink = mask.histogram()[0]
    if not .0002 * pixels <= ink <= .025 * pixels or sum(gray.histogram()[:INK_LEVEL]) < 2 * ink:
        return []
    try:
        sparse = engine.recognize(mask, dpi=dpi, psm=SPARSE_PSM)
    except OcrUnavailable:
        return []
    if sum(bool(_confident(line)) for line in sparse.lines) < 3:
        return []

    excluded = [word.bbox for line in native.lines for word in line.words] + list(tables)
    relaxed = _ink_mask(rgb, BLUE_MARGIN - 4)
    confirmed = []
    checks = 0
    for line in sparse.lines:
        if checks == MAX_CROPS or stop():
            break
        if not _confident(line) or any(_covered(word.bbox, box) for word in line.words for box in excluded):
            continue
        a, b, c, d = map(round, line.bbox)
        margin = max(4, round(.20 * (d - b)))
        crop = relaxed.crop((max(0, a - margin), max(0, b - margin),
                             min(rgb.width, c + margin), min(rgb.height, d + margin)))
        checks += 1
        try:
            reread = engine.recognize(ImageOps.expand(crop, max(10, margin), 255), dpi=dpi, psm=7)
        except OcrUnavailable:
            continue
        if (len(reread.lines) == 1 and _confident(reread.lines[0])
                and reread.lines[0].text.split() == line.text.split()):
            confirmed.append(replace(line, paragraph=("heocr-form-entry", str(len(confirmed)))))
            excluded.append(line.bbox)
    return confirmed
