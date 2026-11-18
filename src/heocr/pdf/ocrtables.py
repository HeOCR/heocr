"""Ruled tables on OCR'd pages (D3e).

On a scanned or image page (T4, T5), table rulings are pixels, not paths. They are found in the
rendered image, and the same grid building as D3b (``tables.find_tables``) turns them into
tables. Then **each cell is OCR'd as its own region**, so a table row cannot vanish inside a
whole-page reading, as it did in the comparison that started the package (a row lost, the page
marked fine):

- **Rulings from pixels.** The page image is thresholded at ``INK_LEVEL``. A horizontal ruling
  is a run of dark pixels at least ``MIN_RULE_PT`` long; consecutive rows of such runs (a thick
  or slightly blurred line) are one ruling at their centre. Vertical rulings are the same on
  the transposed image. Runs are found with a regular expression over each row's bytes, so no
  array library is needed. Short breaks (scan noise, a slight skew) are bridged by the
  collinear joining of D3b.
- **The page around the tables** is OCR'd once with the tables painted white, so no cell text
  is read twice.
- **Each cell** is cropped inside its rulings (``CELL_INSET`` keeps the lines out) and checked
  for ink; a cell without ink is empty without an OCR call. A cell whose ink lies in one band
  of rows is one line, OCR'd with ``--psm 7``; otherwise ``--psm 6`` (a block). With stock
  Tesseract, ``--psm 6`` split a single short word in a wide cell into two lines.
- **Nothing silent.** A cell with ink whose OCR came back empty is flagged
  ``ocr_empty_with_ink``. A row where every inked cell came back empty is recorded in
  ``unread`` with its box (``table_row_ink_unread``), and the table is flagged.
- The table is an OCR block (``source: "ocr"``, ``ocr.status: "unverified"``), with the engine's
  provenance and the cells' mean confidence. Its columns follow the document's direction.
"""

from __future__ import annotations

import re

from heocr.ocr.regions import INK_LEVEL, MIN_INK_PIXELS, MIN_INK_SHARE, has_ink, paper_level
from heocr.pdf.tables import Rule, Table, _merge, find_tables

MIN_RULE_PT = 40.0  # a ruling is at least this long (text strokes and underlined words are shorter)
CELL_INSET_PT = 2.0  # crop this far inside the cell's rulings
CELL_PSM = 6  # a cell of several lines is a uniform block of text
LINE_PSM = 7  # a cell of one line
LINE_GAP_PX = 3  # white pixel rows that separate two lines of a cell
MIN_CELL_PT = 6.0  # a cell narrower or shorter than this is not OCR'd (a doubled border's sliver)


def _runs(data: bytes, width: int, height: int, min_len: int) -> list[tuple[int, int, int]]:
    """Runs of dark pixels per row of a 0/255 image: (row, start, end)."""
    pattern = re.compile(rb"\x00{%d,}" % min_len)
    out = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        if b"\x00" * min_len not in row:
            continue
        for m in pattern.finditer(row):
            out.append((y, m.start(), m.end()))
    return out


def _lines(runs: list[tuple[int, int, int]]) -> list[tuple[float, float, float]]:
    """Runs in consecutive rows that overlap are one line: (centre row, start, end)."""
    open_: list[list] = []  # [first row, last row, start, end]
    done: list[list] = []
    for y, x0, x1 in runs:
        for group in open_:
            if group[1] >= y - 1 and x0 <= group[3] and x1 >= group[2]:
                group[1] = y
                group[2], group[3] = min(group[2], x0), max(group[3], x1)
                break
        else:
            open_.append([y, y, x0, x1])
        still = []
        for group in open_:
            (still if group[1] >= y - 1 else done).append(group)
        open_ = still
    done.extend(open_)
    return [((g[0] + g[1]) / 2, g[2], g[3]) for g in done]


def image_rulings(image, scale: float) -> tuple[list[Rule], list[Rule]]:
    """Horizontal and vertical rulings of a rendered page, in points, top-left origin."""
    gray = image.convert("L")
    binary = gray.point(lambda v: 0 if v < INK_LEVEL else 255)
    min_len = max(int(MIN_RULE_PT * scale), 8)
    w, h = binary.size
    horizontal = [Rule(y / scale, x0 / scale, x1 / scale) for y, x0, x1 in _lines(_runs(binary.tobytes(), w, h, min_len))]
    turned = binary.transpose(method=_TRANSPOSE)
    tw, th = turned.size
    vertical = [Rule(x / scale, y0 / scale, y1 / scale) for x, y0, y1 in _lines(_runs(turned.tobytes(), tw, th, min_len))]
    return _merge(horizontal), _merge(vertical)


try:  # Pillow >= 9.1
    from PIL.Image import Transpose

    _TRANSPOSE = Transpose.TRANSPOSE
except ImportError:  # pragma: no cover
    from PIL import Image as _Image

    _TRANSPOSE = _Image.TRANSPOSE


def tables_in(image, scale: float) -> list[Table]:
    """Ruled tables found in a rendered page, with at least one row."""
    return [t for t in find_tables(*image_rulings(image, scale)) if t.rows]


def cell_crop(image, box, scale: float):
    """The cell's image inside its rulings, or None when the cell is too small to read."""
    x0, y0, x1, y1 = box
    if x1 - x0 < MIN_CELL_PT or y1 - y0 < MIN_CELL_PT:
        return None
    inset = CELL_INSET_PT
    left, top = int((x0 + inset) * scale), int((y0 + inset) * scale)
    right, bottom = int((x1 - inset) * scale), int((y1 - inset) * scale)
    w, h = image.size
    left, top, right, bottom = max(0, left), max(0, top), min(w, right), min(h, bottom)
    if right - left < 4 or bottom - top < 4:
        return None
    return image.crop((left, top, right, bottom))


def cell_has_ink(crop) -> bool:
    return crop is not None and has_ink(crop.convert("L"))


FAINT_INK_DELTA = 40  # grey levels below the cell's own paper that count as faint ink


def cell_has_faint_ink(crop) -> bool:
    """Ink too light for ``has_ink`` (grey above ``INK_LEVEL``) but clearly darker than the cell's own
    paper: a faint row of a scanned table (set C pdf_t5_table_faint: grey 165-196). Every faint text
    cell there passes, and no empty cell."""
    if crop is None:
        return False
    gray = crop.convert("L")
    dark = sum(gray.histogram()[: max(0, paper_level(gray) - FAINT_INK_DELTA)])
    return dark >= max(MIN_INK_PIXELS, MIN_INK_SHARE * gray.size[0] * gray.size[1])


def faint_cell_image(crop):
    """A faint cell's image with its contrast stretched, for OCR."""
    from PIL import ImageOps

    return ImageOps.autocontrast(crop.convert("L"), cutoff=1)


def cell_lines(crop) -> int:
    """Bands of ink rows in a cell image: its number of text lines, roughly."""
    gray = crop.convert("L")
    binary = gray.point(lambda v: 0 if v < INK_LEVEL else 255)
    w, h = binary.size
    data = binary.tobytes()
    bands = 0
    white = LINE_GAP_PX
    for y in range(h):
        if b"\x00" in data[y * w : (y + 1) * w]:
            if white >= LINE_GAP_PX:
                bands += 1
            white = 0
        else:
            white += 1
    return bands


def cell_psm(crop) -> int:
    return LINE_PSM if cell_lines(crop) <= 1 else CELL_PSM
