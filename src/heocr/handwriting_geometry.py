"""Conservative page-level handwriting gate, used only on request."""

from dataclasses import dataclass
import math
from statistics import median

import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree
from scipy.spatial.distance import cdist


LONG_EDGE = 1050
MARGIN = 0.035
MIN_COMPONENTS = 65
MAX_DESCRIPTORS = 480
MIN_HANDWRITING_SCORE = 1.7058496167473047


@dataclass(frozen=True)
class Component:
    box: tuple[int, int, int, int]
    area: int
    stroke: float

    @property
    def width(self):
        return self.box[2] - self.box[0]

    @property
    def height(self):
        return self.box[3] - self.box[1]


def foreground(page):
    image = page.convert("L")
    image.thumbnail((LONG_EDGE, LONG_EDGE), Image.Resampling.LANCZOS)
    width, height = image.size
    if min(width, height) < 100:
        return None
    gray = np.asarray(image, dtype=np.float32)
    margin_x, margin_y = int(MARGIN * width), int(MARGIN * height)
    gray = gray[margin_y : height - margin_y, margin_x : width - margin_x]
    # Local paper estimate limits shadows and illumination gradient.
    background = ndimage.gaussian_filter(gray, sigma=9)
    noise_floor = max(13, 0.20 * np.percentile(np.maximum(background - gray, 0), 99))
    ink = (background - gray >= noise_floor) & (gray < 225)
    if not 0.008 <= float(ink.mean()) <= 0.24:
        return None
    return ink


def components(ink):
    label_image, count = ndimage.label(ink, structure=np.ones((3, 3)))
    sizes = np.bincount(label_image.ravel())
    radius = ndimage.distance_transform_edt(ink)
    maxima = ink & (radius == ndimage.maximum_filter(radius, size=3))
    parts = ndimage.find_objects(label_image)
    glyphs = []
    height, width = ink.shape
    for i in range(count):
        ys, xs = parts[i]
        left, right, top, bottom = xs.start, xs.stop, ys.start, ys.stop
        w, h, area = right - left, bottom - top, int(sizes[i + 1])
        if not (5 <= h <= 0.07 * height and 3 <= w <= 0.08 * width):
            continue
        if area < 9 or not 0.22 <= w / h <= 4 or not 0.09 <= area / (w * h) <= 0.93:
            continue
        piece = label_image[ys, xs] == i + 1
        local = radius[ys, xs][piece & maxima[ys, xs]]
        stroke = float(np.median(local)) if local.size else 1.0
        glyphs.append((i + 1, Component((left, top, right, bottom), area, stroke)))
    return label_image, glyphs


def spread(values):
    p25, p50, p75 = np.percentile(values, (25, 50, 75))
    return float((p75 - p25) / p50) if p50 else 0.0


def baseline_residual(glyphs, height):
    coords = np.array([((c.box[0] + c.box[2]) / 2, c.box[3]) for _, c in glyphs])
    # Bound the neighbor work even on noisy pages with many components.
    sample = np.linspace(0, len(glyphs) - 1, min(900, len(glyphs)), dtype=int)
    weighted = coords * (1, 2.5)
    _, neighbors = cKDTree(weighted).query(weighted[sample], k=min(48, len(glyphs)))
    offsets = coords[neighbors] - coords[sample, None, :]
    dx, dy = offsets[:, :, 0], offsets[:, :, 1]
    nearby = (
        (np.abs(dx) > 0.33 * height)
        & (np.abs(dx) < 4.5 * height)
        & (np.abs(dy) < 0.67 * height)
    )
    penalty = np.abs(dx) + 2.5 * np.abs(dy)
    left_cost = np.where(nearby & (dx < 0), penalty, np.inf)
    right_cost = np.where(nearby & (dx > 0), penalty, np.inf)
    row = np.arange(len(sample))
    left, right = left_cost.argmin(axis=1), right_cost.argmin(axis=1)
    paired = np.isfinite(left_cost[row, left]) & np.isfinite(right_cost[row, right])
    if paired.sum() < 30:
        return None
    a, b = offsets[row, left][paired], offsets[row, right][paired]
    expected = (b[:, 1] * -a[:, 0] + a[:, 1] * b[:, 0]) / (b[:, 0] - a[:, 0])
    return float(np.percentile(np.abs(expected), 70) / height)


def shape_novelty(label_image, glyphs):
    limit = np.linspace(
        0, len(glyphs) - 1, min(MAX_DESCRIPTORS, len(glyphs)), dtype=int
    )
    shapes, sizes = [], []
    for idx in limit:
        i, glyph = glyphs[idx]
        x0, y0, x1, y1 = glyph.box
        mask = np.uint8(label_image[y0:y1, x0:x1] == i) * 255
        resized = Image.fromarray(mask).resize((12, 12), Image.Resampling.BILINEAR)
        shapes.append(np.asarray(resized).ravel() / 255)
        sizes.append((glyph.height, glyph.width / glyph.height))
    distances = cdist(shapes, shapes, metric="cityblock") / 144
    relation = np.abs(np.log(np.array(sizes)[:, None, :] / np.array(sizes)[None, :, :]))
    # Exclude orthographically implausible matches of very different glyph sizes.
    distances[np.any(relation > 0.30, axis=-1)] = np.inf
    np.fill_diagonal(distances, np.inf)
    nearest = distances.min(axis=1)
    active = np.isfinite(nearest)
    if active.sum() < MIN_COMPONENTS * 0.7:
        return None
    return float(np.percentile(nearest[active], 65))


def handwriting_score(page):
    ink = foreground(page)
    if ink is None:
        return None
    labels, glyphs = components(ink)
    if len(glyphs) < MIN_COMPONENTS:
        return None
    height = median(c.height for _, c in glyphs)
    # Body-sized glyphs keep baseline and template matching local.
    body = [(i, c) for i, c in glyphs if 0.62 * height <= c.height <= 1.6 * height]
    if len(body) < MIN_COMPONENTS:
        return None
    jitter = baseline_residual(body, height)
    novelty = shape_novelty(labels, body)
    if jitter is None or novelty is None:
        return None
    stroke_spread = spread([c.stroke / c.height for _, c in body])
    # Median and IQR from the printed calibration pages. The threshold includes
    # a margin above the largest normalized score of any calibration page.
    measurements = (
        (stroke_spread - 0.3727922061357855) / 0.1844038584874671,
        (jitter - 0.07142857142857142) / 0.03885572139303483,
        (novelty - 0.1098325163398693) / 0.023782679738562088,
    )
    score = sum(measurements) / len(measurements)
    return score if math.isfinite(score) else None


def is_handwritten(page):
    """Gate whole scan pages only when all measurements are available."""
    score = handwriting_score(page)
    return score is not None and score >= MIN_HANDWRITING_SCORE
