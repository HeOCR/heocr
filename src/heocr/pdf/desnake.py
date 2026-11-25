"""One logical table from a table printed as side-by-side blocks (a "snake", taxonomy F12; D3g).

A long, narrow table is often printed as k blocks side by side, each under its own copy of the header: "סמל קוד |
התעריף | סמל קוד | התעריף". The record keeps the logical table (0032 gold convention a): the header once, then
block 0's rows, then block 1's, and so on. Block 0 is the start block, the rightmost one in an RTL table; columns
are logical (start first), so it holds the first `p` columns whatever the direction.

A table is de-snaked (``table_desnaked``) when all of these hold:

- its width W splits into k >= 2 equal periods p >= 2;
- its leading header rows (1 or 2) repeat with that period, cell for cell, and are not all empty;
- no cell spans across a period boundary, and no body cell spans rows;
- every block holds at least one filled body row.

Trailing all-empty rows of a block (a short last block) are dropped. Nothing else changes: cell texts and flags are
kept.
"""
from __future__ import annotations

from heocr.contract import Block, Cell

FLAG = "table_desnaked"


def _positions(row: list[Cell]) -> list[tuple[int, Cell]]:
    out, col = [], 0
    for cell in row:
        out.append((col, cell))
        col += cell.colspan
    return out


def _width(rows: list[list[Cell]]) -> int:
    return max((sum(c.colspan for c in row) for row in rows), default=0)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _segment(row: list[Cell], start: int, p: int) -> list[Cell] | None:
    """The row's cells in columns [start, start + p), or None when a cell crosses either edge."""
    out = []
    for col, cell in _positions(row):
        end = col + cell.colspan
        if col < start < end or col < start + p < end:
            return None
        if start <= col < start + p:
            out.append(cell)
    return out


def _period(rows: list[list[Cell]], width: int) -> tuple[int, int] | None:
    """(p, header rows) of the smallest repeating period, or None."""
    for p in range(2, width // 2 + 1):
        if width % p:
            continue
        k = width // p
        header = 0
        for row in rows[:2]:
            segs = [_segment(row, i * p, p) for i in range(k)]
            if any(s is None for s in segs):
                break
            texts = [[_norm(c.text) for c in s] for s in segs]
            if not any(texts[0]) or any(t != texts[0] for t in texts[1:]):
                break
            if any("merged_above" in c.flags for s in segs for c in s):
                break
            header += 1
        if header:
            return p, header
    return None


def desnake(block: Block) -> bool:
    rows = block.cells or []
    width = _width(rows)
    found = _period(rows, width) if width >= 4 else None
    if found is None:
        return False
    p, header = found
    k = width // p
    body = rows[header:]
    if any(c.rowspan > 1 or "merged_above" in c.flags for row in body for c in row):
        return False
    blocks: list[list[list[Cell]]] = []
    for i in range(k):
        segs = [_segment(row, i * p, p) for row in body]
        if any(s is None for s in segs):
            return False
        while segs and not any(c.text.strip() for c in segs[-1]):
            segs.pop()   # a short last block leaves empty rows
        if not segs:
            return False
        blocks.append(segs)
    head = [_segment(row, 0, p) for row in rows[:header]]
    block.cells = [*head, *(seg for b in blocks for seg in b)]
    block.flags.append(FLAG)
    block.flags.append(f"table_blocks_{k}")
    return True
