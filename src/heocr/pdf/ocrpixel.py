"""Recover corroborated horizontal line bands from uncovered scan pixels."""

from __future__ import annotations

from dataclasses import replace
from itertools import accumulate, groupby
from math import ceil, floor
import re
from statistics import mean, median

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from heocr.ocr.regions import paper_level
from heocr.ocr.tesseract import OcrResult, OcrUnavailable
from heocr.pdf.ocrsparse import LINE_PSM, _reading_slot
from heocr.pdf.ocrtables import _runs
from heocr.pdf.perspective import _outlines

MIN_UNCOVERED_INK = 0.72
SHADED_MIN_UNCOVERED_INK = 0.20
SHADE_PAPER_DELTA = 50
SHADE_MAX_SPREAD = 32
MAX_BANDS = 12
THUMBNAIL = 1300
BODY_MARGIN = 0.085
MIN_MEAN_CONFIDENCE = 77
_HEBREW = re.compile(r"[\u05d0-\u05ea]")


def _intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _foreground(gray, window):
    background = gray.filter(ImageFilter.MaxFilter(window)).filter(ImageFilter.MinFilter(window))
    return ImageChops.subtract(background, gray)


def _mask(gray, native, typical, *, preview_size=THUMBNAIL):
    thumb = gray.copy()
    thumb.thumbnail((preview_size, preview_size), Image.Resampling.LANCZOS)
    w, h = thumb.size
    sx, sy = w / gray.width, h / gray.height
    glyph = typical * sy
    foreground = _foreground(thumb, max(5, round(.38 * glyph) | 1))
    mask = foreground.point(lambda v: 0 if v >= 27 else 255)
    draw = ImageDraw.Draw(mask)
    # The original raster stays untouched. Rules and separators should not
    # connect glyph components or serve as text evidence for a proposal.
    horizontal = _runs(mask.tobytes(), w, h, max(12, round(7 * glyph)))
    turned = mask.transpose(Image.Transpose.TRANSPOSE)
    vertical = _runs(turned.tobytes(), h, w, max(12, round(6 * glyph)))
    for y, x0, x1 in horizontal:
        draw.line((x0, y, x1 - 1, y), fill=255)
    for x, y0, y1 in vertical:
        draw.line((x, y0, x, y1 - 1), fill=255)
    for line in native:
        x0, y0, x1, y1 = line.bbox
        draw.rectangle((floor(x0 * sx) - 1, floor(y0 * sy) - 1,
                        ceil(x1 * sx) + 1, ceil(y1 * sy) + 1), fill=255)
    return mask, sx, sy


def _glyphs(mask, height):
    boxes = []
    for outline in _outlines(mask):
        xs, ys = zip(*outline)
        x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
        if not BODY_MARGIN * mask.height < y0 < y1 < (1 - BODY_MARGIN) * mask.height:
            continue
        if .28 * height <= y1 - y0 <= 1.45 * height and 2 <= x1 - x0 <= 1.5 * height:
            boxes.append((x0, y0, x1, y1))
    return boxes


def _bands(boxes, height, minimum_width):
    center = lambda b: (b[1] + b[3]) / 2
    rows = []
    for box in sorted(boxes, key=center):
        if not rows or center(box) - rows[-1][0] > .57 * height:
            rows.append((center(box), []))
        rows[-1][1].append(box)
    for _, row in rows:
        ordered = sorted(row)
        column = 0
        right = 0
        cuts = []
        for box in ordered:
            if box[0] - right > 2.1 * height:
                column += 1
            right = max(right, box[2])
            cuts.append((column, box))
        for _, component in groupby(cuts, key=lambda pair: pair[0]):
            run = [box for _, box in component]
            if len(run) < 8:
                continue
            x0, y0 = min(b[0] for b in run), min(b[1] for b in run)
            x1, y1 = max(b[2] for b in run), max(b[3] for b in run)
            if x1 - x0 < minimum_width or y1 - y0 > 1.55 * height:
                continue
            # A diagonal stamp can contribute a few glyphs to a row, but a
            # full candidate needs a common baseline at both ends.
            side = max(2, len(run) // 4)
            if abs(median(b[3] for b in run[:side]) - median(b[3] for b in run[-side:])) > .31 * height:
                continue
            yield x0, y0, x1, y1


def _read(engine, gray, box, dpi, paper, border):
    x0, y0, x1, y1 = box
    crop = ImageOps.expand(gray.crop(box), border=border, fill=paper)
    result = engine.recognize(crop, dpi=dpi, psm=LINE_PSM)
    if len(result.lines) != 1:
        return None
    line, = result.lines
    if len(line.words) < 3 or len(_HEBREW.findall(line.text)) < 6:
        return None
    if mean(word.confidence for word in line.words) < MIN_MEAN_CONFIDENCE:
        return None
    def page_box(bounds):
        a, b, c, d = bounds
        return [max(x0, a + x0 - border), max(y0, b + y0 - border),
                min(x1, c + x0 - border), min(y1, d + y0 - border)]
    words = [replace(word, bbox=page_box(word.bbox)) for word in line.words]
    if any(w.bbox[0] >= w.bbox[2] or w.bbox[1] >= w.bbox[3] for w in words):
        return None
    return replace(line, bbox=page_box(line.bbox), words=words)


def _shaded(gray, box, paper):
    """Check the tight OCR box, since padding can cross the edge of a filled band."""
    x0, y0, x1, y1 = box
    if x1 - x0 < 3 or y1 <= y0:
        return False
    cuts = [x0 + (x1 - x0) * i // 3 for i in range(4)]
    tones = []
    for left, right in zip(cuts, cuts[1:]):
        histogram = gray.crop((left, y0, right, y1)).histogram()
        target = ceil(.8 * (right - left) * (y1 - y0))
        tones.append(next(value for value, count in enumerate(accumulate(histogram)) if count >= target))
    return max(tones) <= paper - SHADE_PAPER_DELTA and max(tones) - min(tones) <= SHADE_MAX_SPREAD


def recover_pixel_lines(engine, gray, result, scale, outside_share, stop=lambda: False, *, short=True):
    """Add new lines without re-reading or replacing the native text."""
    native = result.lines
    shaded_only = outside_share < MIN_UNCOVERED_INK
    eligible = outside_share >= SHADED_MIN_UNCOVERED_INK if shaded_only else short
    if engine.psm != 3 or not eligible or len(native) < 2 or stop():
        return result
    typical = median(line.bbox[3] - line.bbox[1] for line in native)
    if not .005 * gray.height <= typical <= .06 * gray.height:
        return result
    mask, sx, sy = _mask(gray, native, typical)
    if mask.histogram()[0] > .24 * mask.width * mask.height:
        return result
    proposals = []
    for a, b, c, d in _bands(_glyphs(mask, typical * sy), typical * sy, .15 * mask.width):
        box = (max(0, floor(a / sx) - 3), max(0, floor(b / sy - .16 * typical)),
               min(gray.width, ceil(c / sx) + 3), min(gray.height, ceil(d / sy + .16 * typical)))
        if any(_intersects(box, line.bbox) for line in native):
            continue
        slot = _reading_slot(box, native, gray.width, typical, edge_share=.09)
        if slot is not None:
            proposals.append((-(box[2] - box[0]), box, slot))
    if not proposals:
        return result

    insertions = {}
    accepted = []
    dpi = round(scale * 72)
    paper = 255
    page_paper = None
    for _, box, (index, paragraph) in sorted(proposals)[:MAX_BANDS]:
        if stop():
            break
        if any(_intersects(box, line.bbox) for line in accepted):
            continue
        try:
            a, b, c, d = box
            foreground = _foreground(gray.crop(box), min(25, max(5, round(.38 * typical) | 1)))
            flattened = foreground.point(lambda v: max(0, 255 - 3 * max(0, v - 17)))
            first = _read(engine, flattened, (0, 0, c - a, d - b), dpi, paper, border=10)
            confirm = (_read(engine, flattened, (0, 0, c - a, d - b), dpi, paper, border=20)
                       if first and not stop() else None)
        except OcrUnavailable:
            continue
        if first and confirm and first.text.split() == confirm.text.split():
            offset = lambda xyxy: [v + (a, b)[i % 2] for i, v in enumerate(xyxy)]
            line = replace(first, paragraph=paragraph, bbox=offset(first.bbox),
                           words=[replace(w, bbox=offset(w.bbox)) for w in first.words])
            accepted.append(line)
            if shaded_only:
                if page_paper is None:
                    page_paper = paper_level(gray)
                if not _shaded(gray, line.bbox, page_paper):
                    continue
            insertions.setdefault(index, []).append(line)
    if not insertions:
        return result
    lines = []
    for i, line in enumerate(native):
        lines.extend(sorted(insertions.get(i, ()), key=lambda extra: extra.bbox[1]))
        lines.append(line)
    lines.extend(sorted(insertions.get(len(native), ()), key=lambda extra: extra.bbox[1]))
    return OcrResult(lines)
