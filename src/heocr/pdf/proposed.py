"""Learned table proposals turned into ruled-table grids (D3g).

An optional proposer (``heocr.tableformer``, the ``tables`` extra) finds tables that have no rulings, or only some,
and returns their cell structure. Nothing here reads its text: each proposal becomes the same ``Table`` grid that
D3b builds from rulings, and ``tables.table_blocks`` fills its cells from the page's own text-layer characters with
the usual RTL line building and repairs. heocr stays the only text source.

A proposal is ``{"bbox": [x0, top, x1, bottom], "rows": R, "cols": C, "cells": [{"row", "col", "rowspan",
"colspan", "box"}]}`` in the page's top-left frame (points), columns counted left to right as on the page. Cell
boxes from a detector hug their text, so the grid's outer lines sit on the table's box and each inner line is cut in
the gap between neighbouring rows or columns. With the page's character boxes, the cut goes through the widest part
of the gap that no text crosses, and a text line the detector skipped between two rows (a section label, mostly)
becomes a row of its own (``table_rows_added``). Every character inside the table then falls in exactly one cell.

Native ruled tables have first claim: a proposal that overlaps one is dropped.
"""
from __future__ import annotations

from .tables import Table

PROPOSED = "tableformer_proposed"

Box = tuple[float, float, float, float]


def _clusters(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Overlapping intervals merged, in order."""
    out: list[list[float]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _cut(lo: float, hi: float, ink: list[tuple[float, float]]) -> float:
    """A boundary in the gap (lo, hi) between two bands: the middle of the widest part of the gap no ink crosses, or
    the plain middle when ink fills the gap."""
    if hi <= lo:
        return (lo + hi) / 2
    free, at = [], lo
    for a, b in _clusters([(max(lo, a), min(hi, b)) for a, b in ink if b > lo and a < hi]):
        if a > at:
            free.append((at, a))
        at = max(at, b)
    if at < hi:
        free.append((at, hi))
    if not free:
        return (lo + hi) / 2
    a, b = max(free, key=lambda f: f[1] - f[0])
    return (a + b) / 2


def _bands(n: int, firsts: dict[int, float], lasts: dict[int, float]) -> list[tuple[float, float] | None]:
    return [(firsts[k], lasts[k]) if k in firsts and k in lasts else None for k in range(n)]


def _lines(outer: tuple[float, float], bands: list, ink: list[tuple[float, float]]) -> list[float]:
    """len(bands) + 1 increasing boundaries: the outer ends, and each inner one cut in the gap between neighbouring
    bands (interpolated where a band has no evidence)."""
    n = len(bands)
    lines: list[float | None] = [outer[0]] + [None] * (n - 1) + [outer[1]]
    for k in range(1, n):
        if bands[k - 1] is not None and bands[k] is not None:
            lines[k] = _cut(bands[k - 1][1], bands[k][0], ink)
    known = [i for i, v in enumerate(lines) if v is not None]
    for i, v in enumerate(lines):
        if v is None:
            lo = max(j for j in known if j < i)
            hi = min(j for j in known if j > i)
            lines[i] = lines[lo] + (lines[hi] - lines[lo]) * (i - lo) / (hi - lo)
    for i in range(1, len(lines)):  # keep the lines increasing even when detector boxes overlap
        lines[i] = max(lines[i], lines[i - 1])
    return lines  # type: ignore[return-value]


def _missing_rows(bands: list, spans_across: set[int], top: float, bottom: float,
                  line_spans: list[tuple[float, float]]) -> dict[int, list[tuple[float, float]]]:
    """Text lines inside the table that sit wholly between detected rows (or above the first, below the last),
    keyed by the number of detected rows above them. A gap that a detected cell spans is left alone: its lines
    belong to that cell."""
    known = [b for b in bands if b is not None]
    out: dict[int, list[tuple[float, float]]] = {}
    for lo, hi in line_spans:
        if hi <= top or lo >= bottom or any(lo < b[1] and b[0] < hi for b in known):
            continue
        above = sum(1 for b in known if b[1] <= lo)
        if above in spans_across:
            continue
        out.setdefault(above, []).append((lo, hi))
    return out


def to_table(proposal: dict, ink: list[Box] | None = None) -> Table | None:
    """The proposal as a D3b grid, or None when it is not a table of at least 2 x 2 positions.

    ``ink`` holds the page's character boxes (x0, top, x1, bottom), used only to place grid lines.
    """
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
    inside = [b for b in (ink or []) if x0 <= (b[0] + b[2]) / 2 <= x1 and top <= (b[1] + b[3]) / 2 <= bottom]
    flags = [PROPOSED]
    row_bands = _bands(rows, firsts_y, lasts_y)
    # Rows whose gap below them a detected cell spans: no row may be inserted there.
    spans_across = set()
    for r0, _c0, rs, _cs, _box in cells:
        for k in range(r0, r0 + rs - 1):
            spans_across.add(sum(1 for b in row_bands[:k + 1] if b is not None))
    added = _missing_rows(row_bands, spans_across, top, bottom, _clusters([(b[1], b[3]) for b in inside]))
    if added:
        flags.append("table_rows_added")
        new_index: dict[int, int] = {}
        bands: list = []
        seen = 0
        for k in range(rows + 1):
            if k == rows or row_bands[k] is not None:
                bands.extend(added.pop(seen, []))
            if k < rows:
                new_index[k] = len(bands)
                bands.append(row_bands[k])
                if row_bands[k] is not None:
                    seen += 1
        cells = [(new_index[r0], c0, new_index[r0 + rs - 1] - new_index[r0] + 1, cs, box)
                 for r0, c0, rs, cs, box in cells]
        rows, row_bands = len(bands), bands
    xs = _lines((x0, x1), _bands(cols, firsts_x, lasts_x), [(b[0], b[2]) for b in inside])
    ys = _lines((top, bottom), row_bands, [(b[1], b[3]) for b in inside])
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
    return Table([x0, top, x1, bottom], grid_rows, flags, list(xs))


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def merge_proposals(native: list[Table], proposals: list[dict], ink: list[Box] | None = None) -> list[Table]:
    """Native tables, then each usable proposal that overlaps neither a native table nor an earlier proposal."""
    out = list(native)
    for proposal in proposals:
        table = to_table(proposal, ink)
        if table is not None and not any(_overlaps(table.bbox, t.bbox) for t in out):
            out.append(table)
    return out
