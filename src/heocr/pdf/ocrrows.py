"""Rows of an unruled table on a page read by OCR.

Tesseract reads a page block by block. On an unruled table (amounts, dates and codes in columns, with
no rulings for ``ocrtables`` to find) each column can be a block of its own, so the table came out
column by column: every cell of the first column, then every cell of the next. Its words and numbers
were there, in an order no reader of the page would follow.

This module finds such tables in the OCR result, from geometry only, and gives their cells back in
rows:

- **Pieces.** The page's words are cut into pieces at column gaps (``COLUMN_GAP_HEIGHTS`` line heights,
  at least ``COLUMN_GAP_MIN_PT``), each keeping Tesseract's word order.
- **Rows.** A Tesseract line's pieces stay in one row. A line joins the row it overlaps most, when that
  overlap is at least ``SAME_ROW`` of the smaller height.
- **Runs.** At least ``MIN_ROWS`` consecutive rows, each with two pieces or more, each piece short
  (``MAX_CELL_WORDS`` words: prose in columns has longer lines), each row close to the one before
  (``NEAR`` line heights).
- **Read apart.** A run is a table only when Tesseract read it apart: at least ``SPLIT_ROWS`` of its rows
  are made of two Tesseract lines or more. A table Tesseract read row by row keeps its order.
- **Columns.** The run's gutters, strips free of ink in every row, at least ``GUTTER_HEIGHTS`` line
  heights wide. A row that puts two pieces in one column makes the run unstable, and it is left as
  Tesseract read it.
- **Every word once.** A number Tesseract split over two blocks, and read again whole, can give the same
  reading twice; the repeat is dropped (``duplicates``). A line cut across the table's edge would lose
  words, so such a table is left as read.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from heocr.ocr.tesseract import OcrLine, OcrWord

COLUMN_GAP_HEIGHTS = 2.5
COLUMN_GAP_MIN_PT = 24.0
MIN_ROWS = 3
MAX_CELL_WORDS = 4
NEAR = 1.5  # line heights between one row's bottom and the next row's top
SAME_ROW = 0.5  # two pieces are one row when their vertical overlap is at least this share of the smaller
# A gutter, free of ink in every row of the run, is at least this many line heights wide. It can be
# narrower than a column gap: two date columns side by side are a line height apart, and Tesseract
# already reads them as lines of their own.
GUTTER_HEIGHTS = 0.8
SPLIT_ROWS = 0.5  # at least this share of a run's rows is made of two Tesseract lines or more
LEFTOVER_CHARS = 3  # at most this many characters left over inside a number's box
DUPLICATE_OVERLAP = 0.9  # a piece whose text is in another's, with this share of its box inside the other's, is that piece again


@dataclass
class Piece:
    """Consecutive words of one OCR line with no column gap between them."""

    line: OcrLine
    words: list[OcrWord]

    @property
    def bbox(self) -> list[float]:
        return [min(w.bbox[0] for w in self.words), min(w.bbox[1] for w in self.words),
                max(w.bbox[2] for w in self.words), max(w.bbox[3] for w in self.words)]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class TableRows:
    """A table found in an OCR result: its rows of cells (in reading order), and the lines it took."""

    rows: list[list[list[Piece]]]  # row -> cell -> pieces (a cell can be empty)
    lines: list[OcrLine] = field(default_factory=list)  # the lines it took
    duplicates: int = 0  # pieces given twice (same text, same box), dropped

    @property
    def bbox(self) -> list[float]:
        boxes = [p.bbox for row in self.rows for cell in row for p in cell]
        return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _pieces(line: OcrLine, gap: float) -> list[Piece]:
    """The line cut at column gaps. Each piece keeps Tesseract's word order (its reading order)."""
    order = {id(w): i for i, w in enumerate(line.words)}
    out: list[list[OcrWord]] = []
    reach = None
    for word in sorted(line.words, key=lambda w: w.bbox[0]):
        if out and word.bbox[0] - reach < gap:
            out[-1].append(word)
        else:
            out.append([word])
        reach = word.bbox[2] if reach is None or len(out[-1]) == 1 else max(reach, word.bbox[2])
    return [Piece(line, sorted(group, key=lambda w: order[id(w)])) for group in out]


def _overlap(a: list[float], b: list[float]) -> float:
    inter = min(a[3], b[3]) - max(a[1], b[1])
    return inter / max(1e-6, min(a[3] - a[1], b[3] - b[1]))


def _same(a: Piece, b: Piece) -> bool:
    """One reading given twice. A number Tesseract split over two neighbouring blocks is read again
    whole (``pdf.reader._reread_number_words``): one piece then carries the whole number and its box,
    and the other carries it too, or keeps its own part of it, or a few leftover marks, inside that box."""
    big, small = (a, b) if len(a.text) >= len(b.text) else (b, a)
    # The other piece's own part of the number, or what is left of it: a few marks inside the number's box.
    leftover = len(small.text) <= LEFTOVER_CHARS and any(ch.isdigit() for ch in big.text)
    if small.text not in big.text and not leftover:
        return False
    bb, bs = big.bbox, small.bbox
    ix = min(bb[2], bs[2]) - max(bb[0], bs[0])
    iy = min(bb[3], bs[3]) - max(bb[1], bs[1])
    if ix <= 0 or iy <= 0:
        return False
    return ix * iy >= DUPLICATE_OVERLAP * (bs[2] - bs[0]) * (bs[3] - bs[1])


def _rows(pieces: list[Piece]) -> list[list[Piece]]:
    """Pieces in visual rows. A Tesseract line's pieces stay in one row: when Tesseract read a row as one
    line, its order is kept. A line joins the row it overlaps most, if that overlap is at least
    ``SAME_ROW`` of the smaller height; on a skewed scan a cell can touch the row above as well as its own."""
    units: dict[int, list[Piece]] = {}
    for piece in pieces:
        units.setdefault(id(piece.line), []).append(piece)
    def centre(unit):
        return (min(p.bbox[1] for p in unit) + max(p.bbox[3] for p in unit)) / 2
    rows: list[list[Piece]] = []
    for unit in sorted(units.values(), key=centre):
        best, best_overlap = None, 0.0
        for row in rows[-3:]:  # rows are built top down; only the last few can still take a line
            overlap = max(_overlap(p.bbox, q.bbox) for p in unit for q in row)
            if overlap > best_overlap:
                best, best_overlap = row, overlap
        if best is not None and best_overlap >= SAME_ROW:
            best.extend(unit)
        else:
            rows.append(list(unit))
    return [sorted(row, key=lambda p: p.bbox[0]) for row in rows]


def _top(row) -> float:
    return min(p.bbox[1] for p in row)


def _bottom(row) -> float:
    return max(p.bbox[3] for p in row)


def _gutters(rows: list[list[Piece]], width: float) -> list[tuple[float, float]]:
    spans = sorted((p.bbox[0], p.bbox[2]) for row in rows for p in row)
    left, right = spans[0][0], max(s[1] for s in spans)
    out, reach = [], left
    for x0, x1 in spans:
        if x0 - reach >= width:
            out.append((reach, x0))
        reach = max(reach, x1)
    return [g for g in out if g[0] > left and g[1] < right]


def _band(piece: Piece, gutters) -> int:
    centre = (piece.bbox[0] + piece.bbox[2]) / 2
    return sum(1 for g in gutters if (g[0] + g[1]) / 2 < centre)


def _table(run: list[list[Piece]], width: float, rtl: bool) -> TableRows | None:
    # Tesseract's order is wrong only where it read the columns apart: a row put together from lines of
    # its own. When most rows are one line each (cut at a wide gap), Tesseract read the table row by row,
    # and its order stands.
    if sum(1 for row in run if len({id(p.line) for p in row}) >= 2) < SPLIT_ROWS * len(run):
        return None
    gutters = _gutters(run, width)
    if not gutters:
        return None
    count = len(gutters) + 1
    grid = []
    for row in run:
        cells: list[list[Piece]] = [[] for _ in range(count)]
        for piece in row:
            band = _band(piece, gutters)
            if cells[band]:
                return None  # two pieces of one row in one column: the columns are not stable
            cells[band].append(piece)
        grid.append(cells[::-1] if rtl else cells)
    lines = []
    for row in run:
        for piece in row:
            if not any(piece.line is line for line in lines):
                lines.append(piece.line)
    return TableRows(grid, lines)


def table_rows(lines: list[OcrLine], scale: float, rtl: bool) -> list[TableRows]:
    """Unruled tables in an OCR result whose ``scale`` is pixels per point. A line any of whose words
    falls in a table belongs to it whole: a table is returned only when every word of its lines is
    one of its cells' words."""
    if not lines:
        return []
    heights = [line.bbox[3] - line.bbox[1] for line in lines if line.bbox[3] > line.bbox[1]]
    if not heights:
        return []
    height = statistics.median(heights)
    gap = max(COLUMN_GAP_MIN_PT * scale, COLUMN_GAP_HEIGHTS * height)
    pieces: list[Piece] = []
    twins: list[tuple[Piece, Piece]] = []  # (duplicate, the piece it repeats)
    for piece in (p for line in lines if line.words for p in _pieces(line, gap)):
        twin = next((k for k in pieces if _same(k, piece)), None)
        if twin is None:
            pieces.append(piece)
        elif len(piece.text) > len(twin.text):
            # The whole reading is kept, its part dropped.
            pieces[pieces.index(twin)] = piece
            twins = [(dup, piece if kept is twin else kept) for dup, kept in twins] + [(twin, piece)]
        else:
            twins.append((piece, twin))
    rows = _rows(pieces)
    tables: list[TableRows] = []
    run: list[list[Piece]] = []

    def close():
        if len(run) >= MIN_ROWS:
            table = _table(run, GUTTER_HEIGHTS * height, rtl)
            if table is not None:
                tables.append(table)

    for row in rows:
        cellish = len(row) >= 2 and all(len(p.words) <= MAX_CELL_WORDS for p in row)
        near = run and _top(row) - _bottom(run[-1]) <= NEAR * height
        if cellish and (not run or near):
            run.append(row)
            continue
        close()
        run = [row] if cellish else []
    close()
    # A line cut across a table's edge (part in the table, part out) would lose words: such a table is dropped.
    out = []
    for table in tables:
        held = {id(p) for row in table.rows for cell in row for p in cell}
        # A piece given twice goes with the table that holds its twin, and is not read again.
        repeats = [dup for dup, twin in twins if id(twin) in held]
        for dup in repeats:
            if not any(dup.line is line for line in table.lines):
                table.lines.append(dup.line)
        taken = {id(w) for row in table.rows for cell in row for p in cell for w in p.words} | {id(w) for d in repeats for w in d.words}
        if all(id(w) in taken for line in table.lines for w in line.words):
            table.duplicates = len(repeats)
            out.append(table)
    return out
