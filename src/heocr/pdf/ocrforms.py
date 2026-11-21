"""Preserve corroborated native text in tall cells on scanned forms."""

from __future__ import annotations

from dataclasses import replace
from itertools import groupby
from math import ceil, floor
import re
from statistics import mean

from PIL import ImageOps

from heocr.ocr.regions import paper_level
from heocr.ocr.tesseract import OcrResult, OcrUnavailable
from heocr.pdf.ocrtables import CELL_INSET_PT, LINE_PSM, cell_crop, cell_has_faint_ink, cell_has_ink

TALL_CELL_PT = 40
MIN_CONFIDENCE = 78
MAX_RECHECKS = 6
_HEBREW = re.compile(r"[\u05d0-\u05ea]")


def needs_form_probe(image, grid, scale):
    """Find grids with blank entries or unusually tall cells that can hide field text."""
    blank = tall = False
    for row in grid.rows:
        for cell in row:
            if cell["placeholder"]:
                continue
            box = cell["box"]
            if box[3] - box[1] >= max(90, image.height / (5 * scale)):
                return True
            crop = cell_crop(image, box, scale)
            ink = cell_has_ink(crop) or cell_has_faint_ink(crop)
            blank |= crop is not None and not ink
            tall |= ink and box[3] - box[1] >= TALL_CELL_PT
            if blank and tall:
                return True
    return False


def _credible(line):
    if not line.words or len(_HEBREW.findall(line.text)) < 3:
        return False
    if len(line.words) == 1:
        return line.words[0].confidence >= 86
    return sum(c.isalnum() for c in line.text) >= 6 and mean(w.confidence for w in line.words) >= MIN_CONFIDENCE


def _crosses(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def preserve_form_cell_lines(engine, image, box, scale, result, native, stop):
    """Keep a native line when an isolated read corroborates its text in one tall cell."""
    if not native or box[3] - box[1] < TALL_CELL_PT:
        return result
    x0, y0, x1, y1 = (v * scale for v in box)
    ox = max(0, int((box[0] + CELL_INSET_PT) * scale))
    oy = max(0, int((box[1] + CELL_INSET_PT) * scale))
    offset = lambda b: [v - (ox, oy)[i % 2] for i, v in enumerate(b)]
    originals = result.lines
    joined = " ".join(l.text for l in originals).split()
    accepted = []
    def in_cell(word):
        a, b, c, d = word.bbox
        return x0 <= a < c <= x1 and y0 <= b < d <= y1

    possible = []
    for line in native:
        if not _crosses((x0, y0, x1, y1), line.bbox):
            continue
        for inside, run in groupby(line.words, key=in_cell):
            if inside:
                words = list(run)
                bounds = [bound(w.bbox[i] for w in words) for i, bound in enumerate((min, min, max, max))]
                proposal = replace(line, bbox=bounds, text=" ".join(w.text for w in words), words=words,
                                   confidences=[w.confidence for w in words])
                if _credible(proposal):
                    possible.append(proposal)
    attempted = 0
    for line in possible:
        if attempted >= MAX_RECHECKS or stop():
            break
        local = offset(line.bbox)
        words = line.text.split()
        if any(joined[i:i + len(words)] == words for i in range(len(joined) - len(words) + 1)):
            continue
        if any(_crosses(local, other.bbox) for other in (*originals, *accepted)):
            continue
        a, b, c, d = line.bbox
        crop = image.crop((max(ceil(x0), floor(a) - 2), max(ceil(y0), floor(b) - 2),
                           min(floor(x1), ceil(c) + 2), min(floor(y1), ceil(d) + 2))).convert("L")
        attempted += 1
        try:
            confirmed = engine.recognize(ImageOps.expand(crop, 10, fill=paper_level(crop)),
                                         dpi=round(scale * 72), psm=LINE_PSM).lines
        except OcrUnavailable:
            continue
        if len(confirmed) == 1 and _credible(confirmed[0]) and confirmed[0].text.split() == words:
            accepted.append(replace(line, bbox=local,
                                    words=[replace(w, bbox=offset(w.bbox)) for w in line.words]))
    if not accepted:
        return result
    pending = iter(sorted(accepted, key=lambda line: line.bbox[1]))
    extra = next(pending, None)
    combined = []
    for line in originals:
        while extra is not None and extra.bbox[1] < line.bbox[1]:
            combined.append(extra)
            extra = next(pending, None)
        combined.append(line)
    if extra is not None:
        combined.extend((extra, *pending))
    return OcrResult(combined)
