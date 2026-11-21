"""Reread ink rows in a large gap bounded by existing photo OCR lines."""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from itertools import pairwise
import math
from statistics import mean, median

from heocr.ocr.regions import INK_LEVEL, paper_level
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable
from heocr.pdf.ocrsparse import _overlap, _overlaps_line


@dataclass
class _Strip:
    x: int
    y: int
    middle: float
    slope: float
    image: object
    paper: int

    def page_box(self, box):
        x0, y0, x1, y1 = box
        left, right = self.x + x0, self.x + x1
        left_shift = self.slope * (left - self.middle)
        right_shift = self.slope * (right - self.middle)
        return [
            left,
            self.y + y0 + min(left_shift, right_shift),
            right,
            self.y + y1 + max(left_shift, right_shift),
        ]


def _slope(line: OcrLine) -> float | None:
    centers = sorted((sum(w.bbox[::2]) / 2, sum(w.bbox[1::2]) / 2) for w in line.words)
    centers = centers[:: math.ceil(len(centers) / 32)] if len(centers) > 32 else centers
    reach = 0.25 * (line.bbox[2] - line.bbox[0])
    slopes = [
        (q - y) / (b - x)
        for i, (x, y) in enumerate(centers)
        for b, q in centers[i + 1 :]
        if b - x > max(reach, 1)
    ]
    return median(slopes) if slopes else None


def _edge(line, side, slope, middle):
    return median(
        w.bbox[side] - slope * (sum(w.bbox[::2]) / 2 - middle) for w in line.words
    )


def _strip(image, upper, lower, height) -> _Strip | None:
    from PIL import Image

    a, b = upper.bbox, lower.bbox
    width, canvas_height = image.size
    if min(len(upper.words), len(lower.words)) < 3:
        return None
    overlap = min(a[2], b[2]) - max(a[0], b[0])
    if (
        min(a[2] - a[0], b[2] - b[0]) < 0.20 * width
        or overlap < 0.65 * min(a[2] - a[0], b[2] - b[0])
        or abs(a[2] - b[2]) > 0.06 * width
        or not height <= b[1] - a[3] <= 7 * height
    ):
        return None
    first, last = _slope(upper), _slope(lower)
    if first is None or last is None or abs(first - last) > 0.06:
        return None
    slope = (first + last) / 2
    if not 0.01 <= abs(slope) <= 0.12:
        return None
    pad = 0.4 * height
    left = max(0, math.floor(min(a[0], b[0]) - pad))
    right = min(width, math.ceil(max(a[2], b[2]) + pad))
    if right <= left:
        return None
    middle = (left + right) / 2
    top = math.ceil(_edge(upper, 3, slope, middle) + height * 0.16)
    bottom = math.floor(_edge(lower, 1, slope, middle) - height * 0.16)
    if not 2 * height <= bottom - top <= 7 * height:
        return None
    offset = slope * (left - middle)
    if (
        min(top + offset, top - offset) < 0
        or max(bottom + offset, bottom - offset) > canvas_height
    ):
        return None
    paper = paper_level(image.crop((left, top, right, bottom)))
    rect = image.transform(
        (right - left, bottom - top),
        Image.Transform.AFFINE,
        (1, 0, left, slope, 1, top + offset),
        resample=Image.Resampling.BILINEAR,
        fillcolor=paper,
    )
    return _Strip(left, top, middle, slope, rect, paper)


def _bands(mask, height):
    from PIL import Image

    profile = mask.resize((1, mask.height), Image.Resampling.BOX).tobytes()
    rows = [i for i, ink in enumerate(profile) if ink >= 5]
    if not rows:
        return []
    bounds = []
    start = end = rows[0]
    for row in rows[1:]:
        if row - end > max(1, round(0.12 * height)):
            bounds.append((start, end + 1))
            start = row
        end = row
    bounds.append((start, end + 1))
    return [
        (top, bottom)
        for top, bottom in bounds
        if 0.28 * height <= bottom - top <= 1.5 * height
    ]


def _credible(line):
    return (
        len(line.words) >= 2
        and sum("\u05d0" <= c <= "\u05ea" for c in line.text) >= 6
        and mean(w.confidence for w in line.words) >= 55
    )


def _read_strip(engine, strip, height, dpi, paragraph, page_width):
    from PIL import ImageOps

    image = strip.image
    threshold = min(INK_LEVEL, strip.paper - 30)
    mask = image.point(lambda v: 255 if v < threshold else 0)
    bands = _bands(mask, height)
    if not 2 <= len(bands) <= 3:
        return []
    restored = []
    for top, bottom in bands:
        bounds = mask.crop((0, top, mask.width, bottom)).getbbox()
        if (
            not bounds
            or bounds[2] - bounds[0] < 0.10 * page_width
            or image.width - bounds[2] > 0.08 * page_width
        ):
            return []

        def read(padding):
            pad = round(height * padding)
            left, right = max(0, bounds[0] - pad), min(image.width, bounds[2] + pad)
            upper, lower = max(0, top - pad), min(image.height, bottom + pad)
            margin = max(10, pad)
            crop = ImageOps.expand(
                image.crop((left, upper, right, lower)), border=margin, fill=strip.paper
            )
            return (
                engine.recognize(crop, dpi=dpi, psm=7).lines,
                left - margin,
                upper - margin,
            )

        try:
            primary, dx, dy = read(0.30)
            check, _, _ = read(0.15)
        except OcrUnavailable:
            return []
        if len(primary) != 1 or len(check) != 1 or not _credible(primary[0]):
            return []
        line, corroborated = primary[0], check[0]
        if not top <= (line.bbox[1] + line.bbox[3]) / 2 + dy <= bottom:
            return []
        original = " ".join(line.text.split())
        alternate = " ".join(corroborated.text.split())
        if [c for c in original if c.isdecimal()] != [
            c for c in alternate if c.isdecimal()
        ] or SequenceMatcher(None, original, alternate, autojunk=False).ratio() < 0.90:
            return []

        def page_box(box):
            x0, y0, x1, y1 = box
            return strip.page_box((x0 + dx, y0 + dy, x1 + dx, y1 + dy))

        restored.append(
            replace(
                line,
                bbox=page_box(line.bbox),
                paragraph=paragraph,
                words=[replace(word, bbox=page_box(word.bbox)) for word in line.words],
            )
        )
    return restored


def _same_word_box(box, other):
    overlap = _overlap(box, other, 0) * _overlap(box, other, 1)
    smaller = min(
        (box[2] - box[0]) * (box[3] - box[1]),
        (other[2] - other[0]) * (other[3] - other[1]),
    )
    return overlap > 0 and overlap >= 0.5 * smaller


def _without_read_words(line, occupied_words) -> OcrLine | None:
    """Keep novel words and regenerate the row from their original reading order."""
    words = [
        word
        for word in line.words
        if not any(_same_word_box(word.bbox, box) for box in occupied_words)
    ]
    if not words:
        return None
    if words == line.words:
        return line
    return replace(
        line,
        text=" ".join(word.text for word in words),
        bbox=[
            min(w.bbox[0] for w in words),
            min(w.bbox[1] for w in words),
            max(w.bbox[2] for w in words),
            max(w.bbox[3] for w in words),
        ],
        confidences=[word.confidence for word in words],
        words=words,
    )


def recover_photo_lines(engine, image, result: OcrResult, dpi) -> OcrResult:
    """Retry at most two large, on-track gaps and retain every native line."""
    if engine.psm != 3 or len(result.lines) < 4:
        return result
    heights = [
        line.bbox[3] - line.bbox[1]
        for line in result.lines
        if line.bbox[3] > line.bbox[1]
    ]
    if not heights:
        return result
    height = median(heights)
    restored = []
    occupied = list(result.lines)
    occupied_words = [word.bbox for line in occupied for word in line.words]
    added = 0
    attempts = 0
    for upper, lower in pairwise(result.lines):
        restored.append(upper)
        if attempts == 2:
            continue
        strip = _strip(image, upper, lower, height)
        if strip is None:
            continue
        attempts += 1
        found = _read_strip(engine, strip, height, dpi, lower.paragraph, image.width)
        novel = []
        for line in found:
            remainder = _without_read_words(line, occupied_words)
            if remainder is not None:
                novel.append(remainder)
        if all(
            _credible(line)
            and 0 <= line.bbox[0] < line.bbox[2] <= image.width
            and 0 <= line.bbox[1] < line.bbox[3] <= image.height
            and not any(_overlaps_line(line.bbox, other.bbox) for other in occupied)
            for line in novel
        ):
            restored.extend(novel)
            occupied.extend(novel)
            occupied_words.extend(word.bbox for line in novel for word in line.words)
            added += len(novel)
    if not added:
        return result
    return OcrResult([*restored, result.lines[-1]])
