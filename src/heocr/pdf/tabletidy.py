"""Post-processing of learned table proposals once heocr has filled their cells (D3g).

Only tables flagged ``tableformer_proposed`` are touched: a ruled grid is what the page draws, so it is never
reshaped. Each step is a listed rule, flagged on the table:

- ``table_empty_columns_dropped``: a column whose own (single-column) cells are all empty is removed, and a cell
  spanning it loses one column. Without rulings an all-empty column cannot be seen on the page; it is the detector's.
- ``table_enumerators_joined``: a first column that holds only margin enumerators ("(1)", "2.", "(א)") in a
  minority of rows is joined to the label column: each enumerator prefixes the label beside it, and the column goes
  (0032 gold convention d: an enumerator printed outside the grid joins its row label).
- ``table_section_rows_split``: a row whose first cell holds extra lines above the row's other values (a section
  label such as "(1) ... - קרקע" that the detector merged into the row below it) is split: the label lines become a
  row of their own, label in the first cell and the other cells empty (0032 gold convention c, first-column style).

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
    if split_section_rows(block):
        block.flags.append("table_section_rows_split")
    if drop_empty_columns(block):
        block.flags.append("table_empty_columns_dropped")
