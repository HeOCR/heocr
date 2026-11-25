"""Post-processing of learned table proposals once heocr has filled their cells (D3g).

Only tables flagged ``tableformer_proposed`` are touched: a ruled grid is what the page draws, so it is never
reshaped. Each step is a listed rule, flagged on the table:

- ``table_empty_columns_dropped``: a column whose own (single-column) cells are all empty is removed, and a cell
  spanning it loses one column. Without rulings an all-empty column cannot be seen on the page; it is the detector's.
- ``table_empty_rows_dropped``: a row whose cells are all empty, with no cell spanning into or out of it, is removed:
  without rulings a blank row cannot be seen on the page either.
- ``table_labels_rejoined``: a label printed across a column line at the end of a row (a matrix's diagonal label,
  wider than its column) is one cell again. This applies when the row's last two filled cells hold one line of letters
  each, sit a word space apart on one baseline, and every cell after them is empty: the second joins the first, and its
  cell is left empty.
- ``table_enumerators_joined``: a first column that holds only margin enumerators ("(1)", "2.", "(א)") in a
  minority of rows is joined to the label column: each enumerator prefixes the label beside it, and the column goes
  (0032 gold convention d: an enumerator printed outside the grid joins its row label).
- ``table_section_rows_split``: a row whose first cell holds extra lines above the row's other values (a section
  label such as "(1) ... - קרקע" that the detector merged into the row below it) is split: the label lines become a
  row of their own, label in the first cell and the other cells empty (0032 gold convention c, first-column style).
- ``table_label_lines_joined``: a label printed on two lines, whose first line the detector made a row of its own,
  is one cell again. This applies to a row that holds only one-line letter labels (no digits anywhere), when the
  next row holds values and, under each of those labels, a letter label overlapping it horizontally, and the two
  lines sit closer together than the table's rows do (a line break inside a cell, not a row gap). A section label
  row (convention c) keeps its row: it is a full row gap above the next row.

Cells hold ``Cell`` objects in logical order (first = start column); a position covered by a cell from a row above
is a ``merged_above`` placeholder carrying that cell's colspan.
"""
from __future__ import annotations

import re

from heocr.contract import Block, Cell

PROPOSED = "tableformer_proposed"
# "(1)", "1.", "(א)"; brackets in either orientation, since bracket repair runs after the page is read
ENUMERATOR = re.compile(r"^[()]?(\d{1,3}|[\u05d0-\u05ea]{1,2})[.()]?$")


def _covering(row: list[Cell]):
    """(cell, first column) for each cell in a row, columns in logical order."""
    col = 0
    for cell in row:
        yield cell, col
        col += cell.colspan


def _width(rows: list[list[Cell]]) -> int:
    return max((sum(c.colspan for c in row) for row in rows), default=0)


def drop_empty_columns(block: Block) -> int:
    rows = block.cells
    dropped = 0
    j = 0
    while j < _width(rows) and _width(rows) > 2:
        removable = True
        for row in rows:
            for cell, start in _covering(row):
                if start <= j < start + cell.colspan and cell.colspan == 1 and "merged_above" not in cell.flags \
                        and cell.text.strip():
                    removable = False
        if not removable:
            j += 1
            continue
        for r, row in enumerate(rows):
            out = []
            for cell, start in _covering(row):
                if start <= j < start + cell.colspan:
                    if cell.colspan == 1:
                        continue
                    cell.colspan -= 1
                out.append(cell)
            rows[r] = out
        dropped += 1
    return dropped


def drop_empty_rows(block: Block) -> int:
    rows = block.cells
    spanned = set()  # rows a cell from above reaches into
    for r, row in enumerate(rows):
        for cell in row:
            if cell.rowspan > 1 and "merged_above" not in cell.flags:
                spanned.update(range(r + 1, r + cell.rowspan))
    keep = [row for r, row in enumerate(rows)
            if r in spanned or any(c.text.strip() or c.rowspan > 1 or "merged_above" in c.flags for c in row)]
    dropped = len(rows) - len(keep)
    if dropped and keep:
        block.cells = keep
        return dropped
    return 0


WORD_SPACE = 0.6  # share of the line height: a gap this small between two cells' text is a word space


def _letters(cell: Cell) -> bool:
    text = cell.text.strip()
    return bool(text) and "\n" not in text and not any(ch.isdigit() for ch in text)


def rejoin_spilled_labels(block: Block) -> int:
    joined = 0
    for row in block.cells:
        filled = [i for i, c in enumerate(row) if c.text.strip()]
        if len(filled) < 2:
            continue
        first, second = row[filled[-2]], row[filled[-1]]
        if filled[-1] != filled[-2] + 1 or any("merged_above" in c.flags for c in (first, second)):
            continue
        if not (_letters(first) and _letters(second)) or len(first._lines or []) != 1 or len(second._lines or []) != 1:
            continue
        a, b = _line_box(first._lines[0]), _line_box(second._lines[0])
        if a is None or b is None:
            continue
        height = max(a[3] - a[1], b[3] - b[1], 1.0)
        gap = max(a[0], b[0]) - min(a[2], b[2])   # the space between the two text boxes, in either order
        if abs((a[1] + a[3]) / 2 - (b[1] + b[3]) / 2) > 0.3 * height or gap > WORD_SPACE * height:
            continue
        first.text = f"{first.text.strip()} {second.text.strip()}"
        first._lines = [[first.text, [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]]]
        second.text, second._lines = "", []
        joined += 1
    return joined


def _line_box(line):
    return line[1] if line and len(line) > 1 else None


def split_section_rows(block: Block) -> int:
    rows = block.cells
    out: list[list[Cell]] = []
    split = 0
    for row in rows:
        if not row or any(c.rowspan > 1 or "merged_above" in c.flags for c in row):
            out.append(row)
            continue
        first, others = row[0], row[1:]
        lines = first._lines or []
        filled = [c for c in others if c.text.strip()]
        boxes = [_line_box(line) for c in filled for line in (c._lines or [])]
        if len(lines) < 2 or not filled or any(b is None for b in boxes) or any(len(c._lines or []) != 1 for c in filled):
            out.append(row)
            continue
        values_top = min(b[1] for b in boxes)
        label = [line for line in lines if _line_box(line) and _line_box(line)[3] <= values_top]
        rest = [line for line in lines if line not in label]
        if not label or not rest:
            out.append(row)
            continue
        label_cell = Cell("\n".join(line[0] for line in label), _lines=label)
        out.append([label_cell] + [Cell("", colspan=c.colspan) for c in others])
        first.text = "\n".join(line[0] for line in rest)
        first._lines = rest
        out.append(row)
        split += 1
    block.cells = out
    return split


LINE_BREAK = 0.3  # share of the line height: a label line this much closer than the row gap continues the next row


def _gap(above, below) -> float:
    return below[1] - above[3]


def join_label_lines(block: Block) -> int:
    rows = block.cells
    # The table's usual gap between consecutive rows' first label lines.
    tops = []
    for row in rows:
        boxes = [_line_box(c._lines[0]) for c in row[:1] if c.text.strip() and c._lines]
        tops.append(boxes[0] if boxes and boxes[0] else None)
    gaps = sorted(_gap(a, b) for a, b in zip(tops, tops[1:]) if a and b)
    if len(gaps) < 3:
        return 0
    row_gap = gaps[len(gaps) // 2]
    joined = 0
    r = 0
    while r + 1 < len(rows):
        row, nxt = rows[r], rows[r + 1]
        filled = [c for c in row if c.text.strip()]
        values = [c for c in nxt if any(ch.isdigit() for ch in c.text)]
        plain = all(c.rowspan == 1 and "merged_above" not in c.flags for c in row + nxt)
        if not filled or len(values) < 2 or not plain or not all(_letters(c) and len(c._lines or []) == 1
                                                                 for c in filled):
            r += 1
            continue
        pairs = []
        for cell in filled:
            a = _line_box(cell._lines[0])
            match = None
            for below in nxt:
                if not (_letters(below) and below._lines):
                    continue
                b = _line_box(below._lines[0])
                if a is None or b is None:
                    continue
                height = max(a[3] - a[1], b[3] - b[1], 1.0)
                overlap = min(a[2], b[2]) - max(a[0], b[0])
                if overlap > 0.5 * min(a[2] - a[0], b[2] - b[0]) and _gap(a, b) < row_gap - LINE_BREAK * height:
                    match = below
                    break
            if match is None:
                break
            pairs.append((cell, match))
        if len(pairs) != len(filled):
            r += 1
            continue
        for above, below in pairs:
            below.text = f"{above.text.strip()}\n{below.text.strip()}"
            below._lines = list(above._lines) + list(below._lines or [])
        del rows[r]
        joined += 1
    return joined


def join_enumerators(block: Block) -> int:
    rows = block.cells
    if any(not row or row[0].colspan != 1 or row[0].rowspan != 1 or "merged_above" in row[0].flags or len(row) < 2
           for row in rows):
        return 0
    texts = [row[0].text.strip() for row in rows]
    filled = [t for t in texts if t]
    if not filled or len(filled) * 2 > len(rows) or not all(ENUMERATOR.match(t) for t in filled):
        return 0
    if any(t and "merged_above" in row[1].flags for t, row in zip(texts, rows)):
        return 0
    for t, row in zip(texts, rows):
        if t:
            label = row[1]
            lines = label._lines or []
            label.text = f"{t} {label.text}".strip()
            if lines:
                label._lines = [[f"{t} {lines[0][0]}", lines[0][1]], *lines[1:]]
        row.pop(0)
    return len(filled)


def tidy(block: Block) -> None:
    if PROPOSED not in block.flags:
        return
    if join_enumerators(block):
        block.flags.append("table_enumerators_joined")
    if rejoin_spilled_labels(block):
        block.flags.append("table_labels_rejoined")
    if split_section_rows(block):
        block.flags.append("table_section_rows_split")
    if join_label_lines(block):
        block.flags.append("table_label_lines_joined")
    if drop_empty_columns(block):
        block.flags.append("table_empty_columns_dropped")
    if drop_empty_rows(block):
        block.flags.append("table_empty_rows_dropped")
