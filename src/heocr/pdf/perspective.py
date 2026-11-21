"""Recover an oblique ruled patch without warping the page around it."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import dist
import re

from PIL import Image, ImageFilter

from heocr.ocr.regions import INK_LEVEL, MAX_RENDER_PIXELS, paper_level
from heocr.pdf.ocrtables import MIN_CELL_PT, MIN_RULE_PT, image_rulings
from heocr.pdf.tables import Table, find_tables

# Use a bounded thumbnail to find connected rule networks, then warp the original pixels.
THUMBNAIL = 720
MAX_CANDIDATES = 4
OUTLINE_PAD = 8
INK_RUN = re.compile(rb"\x00+")


def _outlines(binary):
    """A component's outline needs only the endpoints of its horizontal ink runs."""
    width, height = binary.size
    data = binary.tobytes()
    parent = []
    runs = []
    previous = []

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for y in range(height):
        first = 0
        current = []
        for match in INK_RUN.finditer(data[y * width:(y + 1) * width]):
            start, end = match.span()
            index = len(parent)
            parent.append(index)
            runs.append((y, start, end - 1))
            current.append((start, end, index))
            while first < len(previous) and previous[first][1] < start:
                first += 1
            for j in range(first, len(previous)):
                old_start, _, old_index = previous[j]
                if old_start > end:
                    break
                parent[root(index)] = root(old_index)
        previous = current

    outlines = defaultdict(list)
    for index, (y, start, end) in enumerate(runs):
        outlines[root(index)].extend(((start, y), (end, y)))
    return outlines.values()


def _quad(outline):
    """The four diagonal extremes of a moderately tilted, convex rectangular outline."""
    diagonal = lambda p: p[0] + p[1]
    cross_diagonal = lambda p: p[0] - p[1]
    return (min(outline, key=diagonal), max(outline, key=cross_diagonal),
            max(outline, key=diagonal), min(outline, key=cross_diagonal))


def _candidates(image, scale):
    thumb = image.convert("L")
    thumb.thumbnail((THUMBNAIL, THUMBNAIL), Image.Resampling.LANCZOS)
    width, height = thumb.size
    sx, sy = image.width / width, image.height / height
    binary = thumb.point(lambda v: 0 if v < INK_LEVEL else 255).filter(ImageFilter.MinFilter(3))
    quads = []
    for outline in _outlines(binary):
        # Even a one-pixel vertical rule has two endpoints per row on the thumbnail.
        if len(outline) < 2 * MIN_RULE_PT * scale / sy:
            continue
        tl, tr, br, bl = quad = _quad(outline)
        # Reject background, frames cut off by the image edge and degenerate polygons.
        if any(x < 2 or y < 2 or x >= width - 2 or y >= height - 2 for x, y in quad):
            continue
        top, bottom = tr[0] - tl[0], br[0] - bl[0]
        left, right = bl[1] - tl[1], br[1] - tr[1]
        if min(top, bottom) * sx < MIN_RULE_PT * scale or min(left, right) * sy < MIN_RULE_PT * scale:
            continue
        if max(abs(tl[1] - tr[1]), abs(bl[1] - br[1])) > 0.5 * min(top, bottom):
            continue
        if max(abs(tl[0] - bl[0]), abs(tr[0] - br[0])) > 0.5 * min(left, right):
            continue
        quads.append((min(top, bottom) * min(left, right), tuple((x * sx, y * sy) for x, y in quad)))
    return [quad for _, quad in sorted(quads, reverse=True)[:MAX_CANDIDATES]]


def _projection(corners, width, height, pad):
    """Pillow's inverse perspective map from a padded rectangle to the source quad."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = corners
    dx1, dx2, dx3 = x1 - x2, x3 - x2, x0 - x1 + x2 - x3
    dy1, dy2, dy3 = y1 - y2, y3 - y2, y0 - y1 + y2 - y3
    det = dx1 * dy2 - dx2 * dy1
    if abs(det) < 1:
        return None
    g, h = (dx3 * dy2 - dx2 * dy3) / det, (dx1 * dy3 - dx3 * dy1) / det
    a, b = (x1 - x0 + g * x1) / width, (x3 - x0 + h * x3) / height
    d, e = (y1 - y0 + g * y1) / width, (y3 - y0 + h * y3) / height
    g, h = g / width, h / height
    origin = 1 - pad * (g + h)
    if min(origin + g * x + h * y for x in (0, width + 2 * pad) for y in (0, height + 2 * pad)) <= 0:
        return None
    return tuple(value / origin for value in (a, b, x0 - pad * (a + b), d, e, y0 - pad * (d + e), g, h))


def _source_quad(inverse, box):
    a, b, c, d, e, f, g, h = inverse
    x0, y0, x1, y1 = box
    points = []
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        denominator = g * x + h * y + 1
        points.append(((a * x + b * y + c) / denominator, (d * x + e * y + f) / denominator))
    return tuple(points)


@dataclass(frozen=True)
class RectifiedGrid:
    image: Image.Image
    grid: Table
    mask: tuple
    inverse: tuple

    def source_box(self, box, scale):
        """Enclose the four projected corners in the original page coordinate system."""
        xs, ys = zip(*_source_quad(self.inverse, [coordinate * scale for coordinate in box]))
        return [min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale]


def _warp_grid(image, scale, corners):
    tl, tr, br, bl = corners
    width = round((dist(tl, tr) + dist(bl, br)) / 2)
    height = round((dist(tl, bl) + dist(tr, br)) / 2)
    pad = OUTLINE_PAD
    size = (width + 2 * pad, height + 2 * pad)
    if size[0] * size[1] > MAX_RENDER_PIXELS:
        return None
    inverse = _projection(corners, width, height, pad)
    if inverse is None:
        return None
    level = paper_level(image)
    source = image if image.mode in ("L", "RGB") else image.convert("RGB")
    fill = level if source.mode == "L" else (level,) * 3
    patch = source.transform(size, Image.Transform.PERSPECTIVE, inverse, Image.Resampling.BICUBIC, fillcolor=fill)
    horizontal, vertical = image_rulings(patch, scale)
    grids = [grid for grid in find_tables(horizontal, vertical) if grid.rows]
    if len(grids) != 1:
        return None
    grid = grids[0]
    # The detected grid must occupy the whole quadrilateral, with interior support in both axes.
    edge_margin = max(2 * pad, min(width, height) * 0.04) / scale
    if any(abs(a - b) > edge_margin for a, b in zip(grid.bbox, (pad / scale, pad / scale, (width + pad) / scale, (height + pad) / scale))):
        return None
    left, top, right, bottom = grid.bbox
    rows = [h.pos for h in horizontal if top - edge_margin <= h.pos <= bottom + edge_margin
            and h.start <= left + edge_margin and h.end >= right - edge_margin]
    columns = [v.pos for v in vertical if left - edge_margin <= v.pos <= right + edge_margin
               and v.start <= top + edge_margin and v.end >= bottom - edge_margin]
    if len(rows) < 3 or len(columns) < 3 or len(rows) != len(grid.rows) + 1 or len(columns) != len(grid.xs):
        return None
    if any(b - a < MIN_CELL_PT for rules in (rows, columns) for a, b in zip(rules, rules[1:])):
        return None
    if any(min(cell["box"][2] - cell["box"][0], cell["box"][3] - cell["box"][1]) < MIN_CELL_PT
           for row in grid.rows for cell in row if cell["box"] is not None):
        return None
    mask = _source_quad(inverse, [(left - 2) * scale, (top - 2) * scale, (right + 2) * scale, (bottom + 2) * scale])
    return RectifiedGrid(patch, grid, mask, inverse)


def rectify_grid(image, scale):
    """Return a single independently supported recovered grid, or keep the ordinary OCR route."""
    recovered = None
    for corners in _candidates(image, scale):
        result = _warp_grid(image, scale, corners)
        if result is not None:
            if recovered is not None:
                return None  # ambiguous candidates: don't mask either region
            recovered = result
    return recovered
