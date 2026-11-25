"""Learned table proposals turned into ruled-table grids (D3g).

An optional proposer (``heocr.tableformer``, the ``tables`` extra) finds tables that have no rulings, or only some,
and returns their cell structure. Nothing here reads its text: each proposal becomes the same ``Table`` grid that
D3b builds from rulings, and ``tables.table_blocks`` fills its cells from the page's own text-layer characters with
the usual RTL line building and repairs. heocr stays the only text source.

A proposal is ``{"bbox": [x0, top, x1, bottom], "rows": R, "cols": C, "cells": [{"row", "col", "rowspan",
"colspan", "box"}]}`` in the page's top-left frame (points), columns counted left to right as on the page. Cell
boxes from a detector hug their text, so the grid's lines are put midway between neighbouring cells, and the outer
lines on the table's box. Every character inside the table then falls in exactly one cell.

Native ruled tables have first claim: a proposal that overlaps one is dropped.
"""
from __future__ import annotations

from .tables import Table

PROPOSED = "tableformer_proposed"


def _bounds(n: int, outer: tuple[float, float], firsts: dict[int, float], lasts: dict[int, float]) -> list[float]:
    """n + 1 increasing boundaries: outer ends, and each inner one midway between the end of band k - 1 and the
    start of band k. A boundary with no evidence is interpolated between its known neighbours."""
    lines: list[float | None] = [outer[0]] + [None] * (n - 1) + [outer[1]]
    for k in range(1, n):
        if k in firsts and k - 1 in lasts:
            lines[k] = (lasts[k - 1] + firsts[k]) / 2
    known = [i for i, v in enumerate(lines) if v is not None]
    for i, v in enumerate(lines):
        if v is None:
            lo = max(j for j in known if j < i)
            hi = min(j for j in known if j > i)
            lines[i] = lines[lo] + (lines[hi] - lines[lo]) * (i - lo) / (hi - lo)
    for i in range(1, len(lines)):  # keep the lines increasing even when detector boxes overlap
        lines[i] = max(lines[i], lines[i - 1])
    return lines  # type: ignore[return-value]


def to_table(proposal: dict) -> Table | None:
    """The proposal as a D3b grid, or None when it is not a table of at least 2 x 2 positions."""
    rows, cols = int(proposal["rows"]), int(proposal["cols"])
    if rows < 2 or cols < 2:
        return None
    cells = []
    for c in proposal["cells"]:
        r0, c0 = int(c["row"]), int(c["col"])
        rs, cs = max(1, int(c.get("rowspan", 1))), max(1, int(c.get("colspan", 1)))
        if r0 < 0 or c0 < 0 or r0 + rs > rows or c0 + cs > cols:
            continue
        cells.append((r0, c0, rs, cs, c.get("box")))
    x0, top, x1, bottom = proposal["bbox"]
    firsts_x: dict[int, float] = {}
    lasts_x: dict[int, float] = {}
    firsts_y: dict[int, float] = {}
    lasts_y: dict[int, float] = {}
    for r0, c0, rs, cs, box in cells:
        if not box:
            continue
        firsts_x[c0] = min(firsts_x.get(c0, box[0]), box[0])
        lasts_x[c0 + cs - 1] = max(lasts_x.get(c0 + cs - 1, box[2]), box[2])
        firsts_y[r0] = min(firsts_y.get(r0, box[1]), box[1])
        lasts_y[r0 + rs - 1] = max(lasts_y.get(r0 + rs - 1, box[3]), box[3])
    xs = _bounds(cols, (x0, x1), firsts_x, lasts_x)
    ys = _bounds(rows, (top, bottom), firsts_y, lasts_y)
    origin: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for r0, c0, rs, cs, _box in cells:
        span = (r0, c0, rs, cs)
        if any((r, c) in origin for r in range(r0, r0 + rs) for c in range(c0, c0 + cs)):
            continue  # an overlapping detector cell: the first one keeps the positions
        for r in range(r0, r0 + rs):
            for c in range(c0, c0 + cs):
                origin[(r, c)] = span
    for r in range(rows):
        for c in range(cols):
            origin.setdefault((r, c), (r, c, 1, 1))
    grid_rows = []
    for r in range(rows):
        row = []
        for c in range(cols):
            r0, c0, rs, cs = origin[(r, c)]
            if (r0, c0) == (r, c):
                row.append({"box": [xs[c0], ys[r0], xs[c0 + cs], ys[r0 + rs]], "colspan": cs, "rowspan": rs,
                            "placeholder": False, "col": c})
            elif r > r0 and c == c0:
                row.append({"box": None, "colspan": cs, "rowspan": 1, "placeholder": True, "col": c})
        grid_rows.append(row)
    return Table([x0, top, x1, bottom], grid_rows, [PROPOSED], list(xs))


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def merge_proposals(native: list[Table], proposals: list[dict]) -> list[Table]:
    """Native tables, then each usable proposal that overlaps neither a native table nor an earlier proposal."""
    out = list(native)
    for proposal in proposals:
        table = to_table(proposal)
        if table is not None and not any(_overlaps(table.bbox, t.bbox) for t in out):
            out.append(table)
    return out
