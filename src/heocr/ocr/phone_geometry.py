"""Find a photographed paper plane and reread isolated native OCR lines."""

import math
import re
from statistics import median

from PIL import Image, ImageChops, ImageFilter, ImageOps

from heocr.models import MODELS
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, OcrWord
from heocr.pdf.ocrlines import _bands
from heocr.pdf.perspective import _projection, _source_quad

_ANALYSIS_EDGE = 480
_RECOGNITION_EDGE = 1700
_CROP_DPI = 300
_CROP_PADDING = 12
_MAX_LINES = 80
_FAST_NATIVE_CONFIDENCE = 95
_LTR = re.compile(r"[A-Za-z]+|\d+(?:[.,:/-]\d+)*")


def _edge_fit(points, span):
    """Fit a straight edge only when independent samples agree along its length."""
    if len(points) < 8:
        return None
    minimum_span = span * .35
    inliers = []
    for i, (u, v) in enumerate(points):
        for x, y in points[i + 1:]:
            if x - u < minimum_span:
                continue
            slope = (y - v) / (x - u)
            if abs(slope) <= .65:
                intercept = v - slope * u
                support = [(a, b) for a, b in points if abs(b - (slope * a + intercept)) <= 2.5]
                if len(support) > len(inliers):
                    inliers = support
                if len(inliers) == len(points):
                    break
        if len(inliers) == len(points):
            break
    count = len(inliers)
    if count < max(8, .66 * len(points)) or inliers[-1][0] - inliers[0][0] < span * .48:
        return None
    center_x = sum(x for x, _ in inliers) / count
    center_y = sum(y for _, y in inliers) / count
    slope = sum((x - center_x) * (y - center_y) for x, y in inliers)
    slope /= sum((x - center_x) ** 2 for x, _ in inliers)
    return slope, center_y - slope * center_x


def _edge_samples(raster, *, horizontal=False, reverse=False):
    """Find a strong inward luminance step on each ray entering the sheet."""
    if horizontal:
        raster = raster.transpose(Image.Transpose.TRANSPOSE)
    if reverse:
        raster = raster.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    width, height = raster.size
    start, stop = ((.24, .76) if horizontal else (.10, .43))
    first, last = int(height * start), int(height * stop)
    columns = int(width * (.24 if horizontal else .34)) - 4
    if columns <= 0 or last <= first:
        return None
    # These luminance steps are the shifted pixels from the original rays.
    # Differences below zero can be clamped because only strong increases qualify.
    after = raster.crop((8, first, 8 + columns, last))
    before = raster.crop((0, first, columns, last))
    differences = ImageChops.subtract(after, before).tobytes()
    points = []
    for row in range(first, last, max(1, (last - first) // 28)):
        offset = (row - first) * columns
        values = differences[offset:offset + columns]
        strength = max(values)
        if strength >= 16:
            position = 4 + values.rfind(bytes((strength,)))
            points.append((row, width - 1 - position if reverse else position))
    return _edge_fit(points, last - first)


def _analysis_raster(image):
    # Preserve Pillow's thumbnail size and reduction, without copying the
    # already-decoded source before resizing. In an aspect tie use the floor.
    edge = _ANALYSIS_EDGE
    if max(image.size) <= edge:
        return image
    aspect = image.width / image.height
    if aspect <= 1:
        size = edge * aspect
        width = min(math.floor(size), math.ceil(size), key=lambda w: abs(aspect - w / edge))
        target = (max(1, width), edge)
    else:
        size = edge / aspect
        height = min(math.floor(size), math.ceil(size), key=lambda h: abs(aspect - edge / h) if h else 0)
        target = (edge, max(1, height))
    return image.resize(target, Image.Resampling.BOX, reducing_gap=2.0)


def _sheet(image):
    raster = _analysis_raster(image)
    if min(raster.size) < 96:
        return None
    # Ignore the exact intensity of the sheet. Fit its three visible edges,
    # with the bottom of the plane defined within the photographed field.
    smooth = raster.filter(ImageFilter.GaussianBlur(1.1))
    left = _edge_samples(smooth)
    if left is None:
        return None
    right = _edge_samples(smooth, reverse=True)
    if right is None:
        return None
    top = _edge_samples(smooth, horizontal=True)
    if top is None:
        return None
    top_slope, top_offset = top
    if abs(top_slope) > .20:
        return None
    low_offset = raster.height * .78 - top_slope * raster.width / 2

    def intersect(side, offset):
        slope, shift = side
        x = (slope * offset + shift) / (1 - slope * top_slope)
        return x, top_slope * x + offset

    tl, tr = intersect(left, top_offset), intersect(right, top_offset)
    bl, br = intersect(left, low_offset), intersect(right, low_offset)
    top_width, bottom_width = math.dist(tl, tr), math.dist(bl, br)
    if not (.44 * raster.width <= top_width and 1.08 <= bottom_width / top_width <= 1.8
            and tl[0] >= 0 and tr[0] < raster.width and tl[1] >= 0 and tr[1] >= 0
            and -raster.width * .3 <= bl[0] < br[0] <= raster.width * 1.3):
        return None
    sx, sy = image.width / raster.width, image.height / raster.height
    return tuple((x * sx, y * sy) for x, y in (tl, tr, br, bl))


def _inverse(coefficients):
    a, b, c, d, e, f, g, h = coefficients
    matrix = (e - f * h, c * h - b, b * f - c * e, f * g - d,
              a - c * g, c * d - a * f, d * h - e * g, b * g - a * h)
    scale = a * e - b * d
    if abs(scale) < 1e-6:
        return None
    return tuple(value / scale for value in matrix)


def _point(transform, x, y):
    a, b, c, d, e, f, g, h = transform
    scale = g * x + h * y + 1
    return (a * x + b * y + c) / scale, (d * x + e * y + f) / scale


def _center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _level_text(inverse, forward, width, height, lines):
    """Correct the remaining vanishing direction using long native word groups."""
    observations = []
    for line in lines:
        if len(line.words) < 5:
            continue
        points = sorted(_point(forward, *_center(word.bbox)) for word in line.words)
        step = max(1, len(points) // 24)
        points = points[::step]
        slopes = [(by - ay) / (bx - ax) for i, (ax, ay) in enumerate(points)
                  for bx, by in points[i + 1:] if bx - ax >= .19 * width]
        if len(slopes) < 3:
            continue
        slope = median(slopes)
        if abs(slope) < .10 and median(abs(value - slope) for value in slopes) < .008:
            observations.append((median(y for _, y in points), slope))
    if len(observations) < 4:
        return inverse
    observations.sort()
    tilts = [(b - a) / (v - u) for i, (u, a) in enumerate(observations)
             for v, b in observations[i + 1:] if v - u > .12 * height]
    tilt = median(tilts) if tilts else 0
    if abs(tilt) * width > .11:
        return inverse
    slope = median(m - tilt * y for y, m in observations)
    if median(abs(m - slope - tilt * y) for y, m in observations) > .008:
        return inverse

    def source(x, y):
        centered = x - width / 2
        gain = 1 - tilt * centered
        return _point(inverse, width / 2 + centered / gain, (y + slope * centered) / gain)

    quad = tuple(source(x, y) for x, y in ((0, 0), (width, 0), (width, height), (0, height)))
    return _projection(quad, width, height, 0) or inverse


def _rectangles(image, lines, quad):
    """Use native text to locate a single block, then resolve its rectified ink rows."""
    tl, tr, br, bl = quad
    scale = min(2, _RECOGNITION_EDGE / max(image.size))
    width = round(scale * (math.dist(tl, tr) + math.dist(bl, br)) / 2)
    height = round(scale * (math.dist(tl, bl) + math.dist(tr, br)) / 2)
    inverse = _projection(quad, width, height, 0)
    forward = _inverse(inverse) if inverse is not None else None
    if forward is None:
        return None
    native = lines
    inverse = _level_text(inverse, forward, width, height, native)
    forward = _inverse(inverse)
    if forward is None or min(1 + forward[6] * x + forward[7] * y
                              for x in (0, image.width) for y in (0, image.height)) < .2:
        return None
    mapped = []
    word_heights = []
    for line in native:
        points = []
        for word in line.words:
            x, y = _center(word.bbox)
            center = _point(forward, x, y)
            points.append(center)
            if word.bbox[3] > word.bbox[1]:
                word_heights.append(math.dist(center, _point(forward, x, word.bbox[3])) * 2)
        mapped.append(points)
    if len(word_heights) < 16:
        return None
    font = median(word_heights)
    if not 8 <= font <= 60:
        return None
    # The rectified ink pass uses only the text block's horizontal neighborhood.
    # Keep the source line geometry until the result covers every native word.
    centers = [point for row in mapped for point in row]
    if sum(not (0 <= x < width and 0 <= y < height) for x, y in centers) > .06 * len(centers):
        return None
    centers = [(x, y) for x, y in centers if 0 <= x < width and 0 <= y < height]
    top = max(0, math.floor(min(y for _, y in centers) - font * 1.15))
    bottom = min(height, math.ceil(max(y for _, y in centers) + font * 1.65))
    # Limit the block to the area with native support. Read the source directly
    # in the final PSM 7 crops so an analysis warp never becomes an OCR raster.
    left = max(round(width * .055), math.floor(min(x for x, _ in centers) - font * 2))
    right = min(round(width * .945), math.ceil(max(x for x, _ in centers) + font * 2))
    if right - left < width * .28 or bottom - top < font * 5:
        return None

    def crop(box):
        x0, y0, x1, y1 = box
        a, b, c, d, e, f, g, h = inverse
        scale = 1 + g * x0 + h * y0
        coefficients = tuple(value / scale for value in (a, b, a * x0 + b * y0 + c, d, e, d * x0 + e * y0 + f, g, h))
        return image.transform((x1 - x0, y1 - y0), Image.Transform.PERSPECTIVE, coefficients,
                               Image.Resampling.BILINEAR, fillcolor=255)

    probe = crop((left, top, right, bottom))
    paper = median(probe.resize((32, 32), Image.Resampling.BOX).tobytes())
    mask = probe.point(lambda v: 255 if v < min(145, paper - 38) else 0)
    bands = _bands(mask, font)
    if not 5 <= len(bands) <= _MAX_LINES:
        return None
    boxes = []
    for start, end in bands:
        extent = mask.crop((0, start, mask.width, end)).getbbox()
        if extent is None or extent[2] - extent[0] < 2.5 * font:
            continue
        padding = max(3, round(.18 * font))
        boxes.append((max(left, left + extent[0] - padding), max(top, top + start - padding),
                      min(right, left + extent[2] + padding), min(bottom, top + end + padding)))
    if not 5 <= len(boxes) <= _MAX_LINES:
        return None
    return inverse, native, mapped, boxes, crop


def _native_box(inverse, box, size):
    xs, ys = zip(*_source_quad(inverse, box))
    width, height = size
    return [max(0, min(xs)), max(0, min(ys)), min(width, max(xs)), min(height, max(ys))]


def read_lines(engine, image, result, *, line_engine=None, skip_confident=False):
    """Reread a supported prose block and retain the original on uncertainty."""
    if engine.psm != 3 or not 5 <= len(result.lines) <= _MAX_LINES or not all(line.words for line in result.lines):
        return None
    quad = _sheet(image)
    geometry = _rectangles(image, result.lines, quad) if quad is not None else None
    if geometry is None:
        return None
    inverse, native, mapped, boxes, crop = geometry
    # A line has to map to one ink band. A few unassigned lines can survive
    # byte-for-byte as native reads, but an unsupported block cannot replace them.
    by_band = [[] for _ in boxes]
    keep = []
    for line, points in zip(native, mapped, strict=True):
        choices = [i for i, (x0, y0, x1, y1) in enumerate(boxes)
                   if sum(x0 <= x <= x1 and y0 - 3 <= y <= y1 + 3 for x, y in points) >= .75 * len(points)]
        if len(choices) == 1:
            by_band[choices[0]].append((line, points))
        else:
            keep.append((median(y for _, y in points), line))
    used = [i for i, group in enumerate(by_band) if group]
    if len(used) < 5 or sum(len(line.words) for _, line in keep) > .10 * sum(map(len, mapped)):
        return None
    for group, (left, _, right, _) in zip(by_band, boxes, strict=True):
        # Widely separated fragments on the same row can be separate columns.
        spans = sorted((min(x for x, _ in points), max(x for x, _ in points)) for _, points in group)
        if any(b - a > .26 * (right - left) for (_, a), (b, _) in zip(spans, spans[1:])):
            return None
    if line_engine is not None:
        engine = line_engine()
    targets = range(len(boxes))
    # The pinned modern model uses only the LSTM recognizer. The audited
    # engine has no legacy cross-image adaptive classifier for this batch.
    # Keep the original slot sequence for other engines and weights.
    modern = MODELS["heocr_modern_v1"]
    if (getattr(engine, "version", None) == "tesseract 5.5.3"
            and getattr(engine, "models", None) == {modern.name: modern.sha256}):
        targets = tuple(i for i, group in enumerate(by_band)
                        if (group and not any(line.number_readings or line.latin_runs_reread for line, _ in group)
                            and not (skip_confident and len(group) == 1 and group[0][0].confidences
                                     and min(group[0][0].confidences) >= _FAST_NATIVE_CONFIDENCE))
                        or (not group and used[0] < i < used[-1]))
    if not targets:
        return None
    try:
        readings = engine.recognize_line_batch(
            (ImageOps.expand(crop(boxes[i]), _CROP_PADDING, fill=255) for i in targets), _CROP_DPI)
    except OcrUnavailable:
        if line_engine is not None:
            raise ValueError("phone specialist line OCR failed with the pinned model") from None
        return None
    if len(readings) != len(targets):
        if line_engine is not None:
            raise ValueError("phone specialist line OCR returned an incomplete batch")
        return None
    by_slot = dict(zip(targets, readings, strict=True))

    restored = []
    changes = 0
    paragraph = native[0].paragraph
    for i, (box, group) in enumerate(zip(boxes, by_band, strict=True)):
        read = by_slot.get(i)
        seen = [line for line, _ in group]
        y = (box[1] + box[3]) / 2
        if seen:
            paragraph = seen[0].paragraph
        if read is None or len(read.lines) != 1 or not read.lines[0].words:
            restored.extend((y, line) for line in seen)
            continue
        line = read.lines[0]
        if not seen and (sum('א' <= c <= 'ת' for c in line.text) < 6 or
                         not line.confidences or median(line.confidences) < 60 or not used[0] < i < used[-1]):
            continue
        if (any(row.number_readings or row.latin_runs_reread for row in seen)
                or _LTR.findall(" ".join(row.text for row in seen)) != _LTR.findall(line.text)):
            restored.extend((y, row) for row in seen)
            continue
        dx, dy = box[0] - _CROP_PADDING, box[1] - _CROP_PADDING

        def mapped_box(bounds):
            return _native_box(inverse, (bounds[0] + dx, bounds[1] + dy,
                                         bounds[2] + dx, bounds[3] + dy), image.size)

        words = [OcrWord(word.text, mapped_box(word.bbox), word.confidence) for word in line.words]
        restored.append((y, OcrLine(line.text, mapped_box(line.bbox), line.confidences, paragraph, words)))
        changes += 1
    if not changes:
        return None
    restored.extend(keep)
    return OcrResult([line for _, line in sorted(restored, key=lambda row: row[0])])
