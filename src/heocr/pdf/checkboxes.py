"""Recover corroborated blue ticks inside printed checkbox frames."""

from __future__ import annotations

from statistics import median

from PIL import Image

from heocr.contract import Block, Line, time_up
from heocr.ocr.regions import paper_level
from heocr.pdf.formtext import BLUE_MARGIN, _ink_mask
from heocr.pdf.perspective import _outlines
from heocr.pdf.ocrsparse import _overlap

MIN_FRAME_PT = 4.0
MAX_FRAME_PT = 11.0
SAMPLE_EDGE = 24


def _frame_state(frame, colour):
    """Return evidence for an empty outline and for a check stroke."""
    px = frame.load()

    def edge_rows(rows, transpose=False):
        def ink(i, j):
            return px[j, i] == 0 if transpose else px[i, j] == 0

        return sum(any(ink(i, j) for j in rows) for i in range(2, 22))

    top, bottom = edge_rows(range(4)), edge_rows(range(20, 24))
    left, right = edge_rows(range(4), transpose=True), edge_rows(range(20, 24), transpose=True)
    if min(bottom, right) < 17 or max(top, left) < 6:
        return None

    dark = [(x, y) for x in range(3, 19) for y in range(3, 19) if px[x, y] == 0]
    blue = colour.crop((3, 3, 19, 19)).histogram()[0]
    if blue < 8 or not 12 <= len(dark) <= 95:
        return len(dark) < 7, False
    bands = [[y for x, y in dark if x in band] for band in (range(3, 8), range(8, 13), range(13, 19))]
    if not all(bands):
        return False, False
    entry, bend, exit = (median(band) for band in bands)
    return False, 9 <= entry <= 17 and 11 <= bend <= 18 and entry <= bend + 1 and bend - exit >= 4


def checked_boxes(rgb, scale):
    """Find boxes with an angled coloured stroke and companion empty outlines."""
    coloured = _ink_mask(rgb, BLUE_MARGIN)
    if coloured.getextrema()[0] != 0:
        return []
    gray = rgb.convert("L")
    cutoff = max(150, paper_level(gray) - 48)
    binary = gray.point(lambda value: 0 if value < cutoff else 255)
    marked = []
    frames = empty = 0
    for outline in _outlines(binary):
        left = min(x for x, _ in outline)
        top = min(y for _, y in outline)
        right = max(x for x, _ in outline) + 1
        bottom = max(y for _, y in outline) + 1
        width, height = right - left, bottom - top
        low, high = min(width, height), max(width, height)
        if not MIN_FRAME_PT * scale <= low <= high <= MAX_FRAME_PT * scale or low < .76 * high:
            continue
        box = left, top, right, bottom
        patches = [image.crop(box).resize((SAMPLE_EDGE, SAMPLE_EDGE), Image.Resampling.NEAREST)
                   for image in (binary, coloured)]
        evidence = _frame_state(*patches)
        if evidence is None:
            continue
        blank, tick = evidence
        frames += 1
        empty += blank
        if tick:
            marked.append([v / scale for v in box])
    if frames < 3 or empty < 2:
        return []
    return sorted(marked, key=lambda box: (box[1], -box[0]))


def append_checkmarks(doc, index, rgb, scale):
    """Keep a located checked glyph as a block without editing native text."""
    if time_up(doc):
        return
    page = doc.pages[-1]
    if page.index != index or page.route != "ocr":
        return
    known = [line.bbox for block in doc.blocks if block.page == index
             for line in block.lines or () if any(ch in "✓✔☑" for ch in line.text)]
    found = []
    for box in checked_boxes(rgb, scale):
        if any(_overlap(box, old, 0) and _overlap(box, old, 1) for old in known):
            continue
        found.append(box)
    direction = "rtl" if doc._rtl else "ltr"
    doc.blocks.extend(Block("text", "☑", page=index, bbox=box, source="ocr",
                            tool="heocr", direction=direction, lines=[Line("☑", box, direction)],
                            flags=["ocr_checkbox", "ocr_unverified"])
                      for box in found)
    if found:
        page.coverage["ocr_checked_boxes"] = len(found)
        page.coverage["chars_ocr"] += len(found)
