"""Recover ruled grids from the photographed paper plane before cell OCR."""

from math import dist
import re

from PIL import Image, ImageFilter

from heocr.ocr.phone_geometry import _sheet
from heocr.ocr.regions import INK_LEVEL
from heocr.pdf.ocrtables import MIN_CELL_PT, MIN_RULE_PT, _grid_continues, _lines, tables_in
from heocr.pdf.perspective import RectifiedGrid, _projection, _source_quad
from heocr.pdf.tables import JOIN, Rule, _merge, find_tables

_PREVIEW_EDGE = 720
_MAX_GRIDS = 4
_INK = re.compile(rb"\x00+")


def _rulings(binary, scale, min_length):
    """Join short gaps in narrow strokes. Long runs need 75% ink support."""
    width, height = binary.size
    min_pixels = max(8, round(min_length * scale))
    gap = max(1, round(2 * scale))
    pixels = binary.tobytes()
    runs = []
    for y in range(height):
        row = pixels[y * width:(y + 1) * width]
        if row.count(0) < .75 * min_pixels:
            continue
        start = end = count = 0
        for match in _INK.finditer(row):
            left, right = match.span()
            if left - end > gap:
                if end - start >= min_pixels and count >= .75 * (end - start):
                    runs.append((y, start, end))
                start, count = left, 0
            if count == 0:
                start = left
            end = right
            count += right - left
        if end - start >= min_pixels and count >= .75 * (end - start):
            runs.append((y, start, end))
    return _merge([Rule(y / scale, x0 / scale, x1 / scale) for y, x0, x1 in _lines(runs)])


def _narrow_grids(preview, scale):
    # Expand the evidence by one preview pixel to tolerate residual slant.
    # The expanded raster is used only to find rules, never to read glyphs.
    binary = preview.point(lambda value: 0 if value < INK_LEVEL else 255).filter(ImageFilter.MinFilter(3))
    horizontal = _rulings(binary, scale, MIN_RULE_PT)
    vertical = _rulings(binary.transpose(Image.Transpose.TRANSPOSE), scale, 2 * MIN_CELL_PT)
    # A short stem touching one rule is common in handwritten or tightly
    # typeset cells. Require support from two separate row boundaries.
    vertical = [v for v in vertical
                if sum(v.start - JOIN <= h.pos <= v.end + JOIN
                       and h.start - JOIN <= v.pos <= h.end + JOIN for h in horizontal) >= 2]
    return [grid for grid in find_tables(horizontal, vertical, require_closed=True)
            if len(grid.rows) >= 2 and len(grid.xs) >= 3
            and not _grid_continues(grid, horizontal, vertical, MIN_RULE_PT)]


def _disjoint(box, others):
    left, top, right, bottom = box
    return all(right <= x0 or x1 <= left or bottom <= y0 or y1 <= top for x0, y0, x1, y1 in others)


def grids_from_phone(image):
    """Return bounded, nonoverlapping grids with a map to the original page."""
    quad = _sheet(image)
    if quad is None:
        return []
    tl, tr, br, bl = quad
    width = round((dist(tl, tr) + dist(bl, br)) / 2)
    height = round((dist(tl, bl) + dist(tr, br)) / 2)
    # Avoid using the photographed paper edges themselves as table rulings.
    # The plane is already supported by those edges. Its interior must also
    # contain horizontal and vertical evidence for a grid.
    inverse = _projection(quad, width, height, 0)
    if inverse is None:
        return []
    factor = min(1, _PREVIEW_EDGE / max(width, height))
    inset = round(width * .025)
    inner = _source_quad(inverse, (inset, inset, width - inset, height - inset))
    width, height = width - 2 * inset, height - 2 * inset
    preview_size = (round(width * factor), round(height * factor))
    preview_map = _projection(inner, *preview_size, 0)
    if preview_map is None:
        return []
    preview = image.transform(preview_size, Image.Transform.PERSPECTIVE, preview_map, Image.Resampling.BICUBIC, fillcolor=255)
    # Camera DPI metadata often describes the JPEG rather than the paper.
    # Use a width-normalized scale so a long ruling is about a tenth
    # of the page even when the camera DPI is missing or inaccurate.
    cell_scale = image.width / 420
    preview_scale = cell_scale * factor
    grids = [grid for grid in tables_in(preview, preview_scale)
             if "table_open_sides_inferred" not in grid.flags]
    occupied = [grid.bbox for grid in grids]
    for grid in _narrow_grids(preview, preview_scale):
        if _disjoint(grid.bbox, occupied):
            occupied.append(grid.bbox)
            grids.append(grid)
    if not 0 < len(grids) <= _MAX_GRIDS:
        return []
    # Reuse the same geometry at native OCR scale. PDF-point grid coordinates
    # remain valid because the preview scale shrinks with the raster.
    projection = _projection(inner, width, height, 0)
    if projection is None:
        return []
    patch = image.transform((width, height), Image.Transform.PERSPECTIVE, projection, Image.Resampling.BICUBIC, fillcolor=255)
    candidates = []
    for grid in sorted(grids, key=lambda item: (item.bbox[1], item.bbox[0])):
        left, top, right, bottom = grid.bbox
        mask = _source_quad(projection, [(left - 2) * cell_scale, (top - 2) * cell_scale,
                                         (right + 2) * cell_scale, (bottom + 2) * cell_scale])
        if all(0 <= x < image.width and 0 <= y < image.height for x, y in mask):
            candidates.append(RectifiedGrid(patch, grid, mask, projection, cell_scale))
    return candidates
