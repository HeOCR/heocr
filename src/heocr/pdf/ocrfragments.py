"""Corroborate residual overlapping fragments and narrow scan lines."""

from __future__ import annotations

from dataclasses import replace
from math import ceil, floor
from statistics import mean, median

from PIL import ImageOps

from heocr.ocr.regions import paper_level
from heocr.ocr.tesseract import OcrResult, OcrUnavailable
from heocr.pdf.ocrpixel import THUMBNAIL, _HEBREW, _bands, _foreground, _glyphs, _intersects, _mask
from heocr.pdf.ocrsparse import LINE_PSM, _overlap

MAX_LINE_CHECKS = 6
NARROW_MIN_UNCOVERED_INK = .12
NARROW_MIN_WIDTH = .055
NARROW_MAX_WIDTH = .15


def _covered(box, other):
    area = (box[2] - box[0]) * (box[3] - box[1])
    return _overlap(box, other, 0) * _overlap(box, other, 1) / max(1, area)


def _confidence(line):
    return mean(word.confidence for word in line.words) if line.words else 0


def _covers_words(line, fragment):
    return all(any(_covered(word.bbox, other.bbox) >= .60 for other in line.words) for word in fragment.words)


def _nested(fragment, body, typical):
    if (not 1 <= len(fragment.words) <= 2 or len(body.words) < 3
            or fragment.number_readings or fragment.numbers_replaced or fragment.latin_runs_reread
            or _covered(fragment.bbox, body.bbox) < .96
            or fragment.bbox[3] - fragment.bbox[1] > .72 * (body.bbox[3] - body.bbox[1])):
        return False
    if _confidence(body) < 85 or _confidence(body) - _confidence(fragment) < 12:
        return False
    return (all(word.bbox[2] - word.bbox[0] <= .24 * typical for word in fragment.words)
            and _covers_words(body, fragment))


def _read(engine, crop, box, dpi, border, paper):
    image = ImageOps.expand(crop, border=border, fill=paper)
    result = engine.recognize(image, dpi=dpi, psm=LINE_PSM)
    if len(result.lines) != 1:
        return None
    line, = result.lines
    if any(word.confidence < 65 for word in line.words) or _confidence(line) < 85:
        return None
    x0, y0, x1, y1 = box

    def mapped(bounds):
        a, b, c, d = bounds
        return [max(x0, a + x0 - border), max(y0, b + y0 - border),
                min(x1, c + x0 - border), min(y1, d + y0 - border)]

    words = [replace(word, bbox=mapped(word.bbox)) for word in line.words]
    if any(a >= c or b >= d for a, b, c, d in (word.bbox for word in words)):
        return None
    return replace(line, bbox=mapped(line.bbox), words=words)


def _confirmed(engine, gray, box, dpi, typical, stop):
    crop = gray.crop(box)
    try:
        raw = _read(engine, crop, box, dpi, 30, paper_level(crop))
        if raw is None or stop():
            return None
        foreground = _foreground(crop, min(25, max(5, round(.38 * typical) | 1)))
        flattened = foreground.point(lambda v: max(0, 255 - 3 * max(0, v - 17)))
        contrast = _read(engine, flattened, box, dpi, 20, 255)
    except OcrUnavailable:
        return None
    if not contrast or contrast.text.split() != raw.text.split() or len(contrast.words) != len(raw.words):
        return None
    if all(_covered(a.bbox, b.bbox) >= .65 and _covered(b.bbox, a.bbox) >= .65
           for a, b in zip(raw.words, contrast.words, strict=True)):
        return contrast
    return None


def suppress_contained_fragments(engine, gray, result, scale, stop=lambda: False):
    """Keep a corroborated full line and discard a contained fragment of its printed words."""
    lines = result.lines
    if engine.psm != 3 or len(lines) < 2 or stop():
        return result
    typical = median(line.bbox[3] - line.bbox[1] for line in lines)
    removed = set()
    confirmed = set()
    checks = 0
    for i, (a, b) in enumerate(zip(lines, lines[1:])):
        if checks >= MAX_LINE_CHECKS or stop():
            break
        if a.paragraph != b.paragraph or i in removed or i + 1 in removed:
            continue
        pair = next(((part, whole, j, k) for part, whole, j, k in ((a, b, i, i + 1), (b, a, i + 1, i))
                     if j not in confirmed and _nested(part, whole, typical)), None)
        if pair is None:
            continue
        fragment, body, j, k = pair
        x0, y0, x1, y1 = body.bbox
        box = (max(0, floor(x0) - 3), max(0, floor(y0) - 3),
               min(gray.width, ceil(x1) + 3), min(gray.height, ceil(y1) + 3))
        if any(_intersects(box, line.bbox) for index, line in enumerate(lines) if index not in (i, i + 1)):
            continue
        checks += 1
        reread = _confirmed(engine, gray, box, round(scale * 72), typical, stop)
        if reread and reread.text.split() == body.text.split() and _covers_words(reread, fragment):
            removed.add(j)
            confirmed.add(k)
    return OcrResult([line for i, line in enumerate(lines) if i not in removed]) if removed else result


def _mid_y(box):
    return (box[1] + box[3]) / 2


def _narrow_slot(box, lines, typical):
    middle = _mid_y(box)
    neighbors = [i for i, line in enumerate(lines)
                 if len(line.words) >= 2 and abs(_mid_y(line.bbox) - middle) <= 3 * typical]
    # A gap in engine order can indicate another column. Keep such cases alone.
    if not neighbors or neighbors != list(range(neighbors[0], neighbors[-1] + 1)):
        return None
    closest = min(neighbors, key=lambda i: abs(_mid_y(lines[i].bbox) - middle))
    return closest + int(_mid_y(lines[closest].bbox) <= middle)


def recover_narrow_lines(engine, gray, result, scale, outside_share, stop=lambda: False, *, preview_size=THUMBNAIL):
    """Recover isolated narrow prose after two confident, agreeing crop reads."""
    lines = result.lines
    if engine.psm != 3 or outside_share < NARROW_MIN_UNCOVERED_INK or len(lines) < 2 or stop():
        return result
    typical = median(line.bbox[3] - line.bbox[1] for line in lines)
    if not .005 * gray.height <= typical <= .06 * gray.height:
        return result
    mask, sx, sy = _mask(gray, lines, typical, preview_size=preview_size)
    if mask.histogram()[0] > .24 * mask.width * mask.height:
        return result
    proposals = []
    for a, b, c, d in _bands(_glyphs(mask, typical * sy), typical * sy, NARROW_MIN_WIDTH * mask.width):
        box = (max(0, floor(a / sx) - 3), max(0, floor(b / sy - .16 * typical)),
               min(gray.width, ceil(c / sx) + 3), min(gray.height, ceil(d / sy + .16 * typical)))
        if not .12 * gray.width <= box[0] < box[2] <= .96 * gray.width:
            continue
        if box[2] - box[0] >= NARROW_MAX_WIDTH * gray.width or any(_intersects(box, line.bbox) for line in lines):
            continue
        index = _narrow_slot(box, lines, typical)
        if index is not None:
            proposals.append((box, index))

    additions = []
    for box, index in sorted(proposals, key=lambda pair: pair[0][0] - pair[0][2])[:MAX_LINE_CHECKS]:
        if stop():
            break
        line = _confirmed(engine, gray, box, round(scale * 72), typical, stop)
        if (line and len(line.words) >= 3 and len(_HEBREW.findall(line.text)) >= 6
                and not any(ch.isdecimal() for ch in line.text)
                and not any(_intersects(line.bbox, other.bbox) for _, other in additions)):
            # A left-side line can widen a native paragraph and change the
            # subsequent block ordering. Keep the new line in its own paragraph.
            additions.append((index, replace(line, paragraph=("heocr-narrow", str(len(additions))))))
    if not additions:
        return result
    recovered = list(lines)
    for index, line in sorted(additions, key=lambda pair: (pair[0], pair[1].bbox[1]), reverse=True):
        recovered.insert(index, line)
    return OcrResult(recovered)
