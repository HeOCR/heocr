"""Unruled tables and side-by-side text (D3c).

A text-layer page whose rows hold several side-by-side pieces was flagged
``reading_order_unresolved_columns`` and read row by row. This module looks at each **run** of
such rows and decides what the run is, from geometry only. Text always comes from the layer,
and every character ends up in a line or a cell:

- **Rows next to a ruled table** continue it. Word draws some rows without borders, so a
  ruled table can stop at such a row and resume below it. Side-by-side rows at the table's edge
  whose pieces each fit one of the table's columns become rows of that table, and two ruled
  tables with the same columns that these rows bridge become one table (``table_rows_unruled``).
- **Column bands.** The bands of a run are found from its **gutters**: vertical strips free of
  ink in every row of the run. A run is extended over a following row only while **all** its
  gutters survive: a row that removes one (a long piece across the run's columns), or that has
  more pieces than the run has bands, starts a run of its own, so no row is left with more
  pieces than bands. A run never
  ends on a row with a single piece. A run whose row still puts two pieces in one band is
  unstable, and stays flagged.
- **Running text in columns:** two or three bands, at least ``MIN_FLOW_ROWS`` rows, and lines
  that fill their band, as prose does. Each band is read as its own flow, the right band first
  in an RTL document. The order is still geometric, so the page says
  ``reading_order_column_major_unverified``.
- **Not a table, but text in columns:** prose beside text or a list, or two lists side by side
  (``PROSE_WORDS``), and a "table" whose cells are mostly empty (``SPARSE``: labels scattered over a
  chart). Their order is not known: the run stays flagged.
- **An unruled table:** any other stable run of at least ``MIN_TABLE_ROWS`` rows. Its cells are
  the bands of each row, in the document's direction. A line alone in one band, closer to the
  row above than rows are to each other, continues that row's cell. The block is flagged
  ``unruled_table``.
- **A list with its labels in the margin** (hanging indent: ``2.16.`` at the right, the
  paragraph indented beside it): when the band read first holds only list labels, the run is
  read row by row, so each label stays in front of its text. Read as a table, its paragraphs
  would break into one cell per line.
- **One or two side-by-side rows** (forms, "label: value", signatures) with no other text in
  their bands just above or below: row by row and column by column give the same order, so
  they are read row by row and flagged ``reading_order_row_major_unverified``.
- **Ambiguity stays flagged.** When the line just above or below a run could be one of its
  columns' lines, or the bands are not stable, nothing is decided and the run keeps
  ``reading_order_unresolved_columns``. A neighbouring line is such a line only when it fits
  inside one band, **lines up** with that band's piece in the adjacent row (the reading-side
  edge or the centre, within ``ALIGN`` line heights), and is **no further** from the run than
  the run's rows are from each other (plus ``PITCH_SLACK``). A heading or a paragraph's last
  line above a signature block is neither, and no longer holds the block back.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from heocr.contract import Block, Cell
from heocr.pdf.lines import COLUMN_GAP_HEIGHTS, COLUMN_GAP_MIN, Atom, BuiltLine, build_lines
from heocr.pdf.tables import CellRepairs

TOOL = "heocr.pdf"
MIN_TABLE_ROWS = 3
MIN_FLOW_ROWS = 6
MAX_FLOW_BANDS = 3
FLOW_FILL = 0.6  # a prose line fills at least this share of its band
FLOW_LINES = 0.6  # and at least this share of a band's lines are such lines
FLOW_BAND = 0.25  # every band of running text is at least this share of the run's width (not page numbers)
GUTTER_SHARE = 0.5  # a gutter is at least this share of the column gap that split the rows
NEAR = 1.5  # line heights: rows this close to each other, or to a table's edge, belong together
FOOTER_BAND = 0.1  # share of the page height at its bottom: the furniture band (furniture.BAND)
EDGE = 3.0  # pt: tolerance on a ruled table's column positions
CONTINUATION = 0.75  # a lone line closer than this share of the usual row gap continues the cell above
ALIGN = 0.5  # line heights: a neighbouring line lines up with a band's piece when an edge or the centre is this close
PITCH_SLACK = 0.25  # line heights: and it is no further from the run than the run's own rows are from each other
# Text in columns, not a table: one band reads as prose (at least PROSE_SHARE of its lines have
# PROSE_WORDS words or more) beside another band of text (TEXT_WORDS words a line on average) or of
# list items, or two bands of list items (at least LIST_SHARE of their lines start with a marker).
PROSE_WORDS = 5
PROSE_SHARE = 0.5
TEXT_WORDS = 3.5
LIST_SHARE = 0.5
SPARSE = 0.6  # a "table" with more than this share of empty cells is scattered labels (a chart), not records
MARKER = re.compile(r"^\s*(?:[•·▪●○■□◦❑\-–*]|\(?\d+[.)]|\(?[א-ת][.)])\s")

UNRESOLVED = "reading_order_unresolved_columns"
LABEL = re.compile(r"^[(\[]?(?:\d+[.)\]]|[.]?\d+(?:[.]\d+)+[.)]?)$|^[(]?[א-תa-zA-Z][.)]$|^[•·▪●○■□◦\-–*]$")  # not a bare number


@dataclass
class Segment:
    """A part of the page in reading order: lines, a column flow, or a table."""

    kind: str  # "lines", "rows", "flow", "table"
    atoms: list[Atom] = field(default_factory=list)
    bands: list[list[Atom]] = field(default_factory=list)  # "flow": each band's atoms in reading order; "rows": each row's
    block: Block | None = None  # "table"
    chars: int = 0  # "table": characters placed in its cells


@dataclass
class _Row:
    lines: list[BuiltLine]

    @property
    def top(self) -> float:
        return min(line.bbox[1] for line in self.lines)

    @property
    def bottom(self) -> float:
        return max(line.bbox[3] for line in self.lines)

    @property
    def multi(self) -> bool:
        return len(self.lines) > 1

    @property
    def atoms(self) -> list[Atom]:
        return [a for line in self.lines for a in line.atoms]


def _ink(atoms: list[Atom]) -> int:
    return sum(1 for a in atoms if not a.is_space)


def arrange(
    atoms: list[Atom],
    rtl: bool,
    page_index: int,
    ruled: list[tuple[Block, list[float]]],
    repairs_on: bool = True,
    page_height: float | None = None,
) -> tuple[list[Segment], list[Block], int]:
    """Split a page's body characters into segments in reading order.

    Returns the segments, the ruled table blocks (some extended or merged), and the characters
    moved from lines into ruled tables.
    """
    built = build_lines(atoms)
    tables = [block for block, _ in ruled]
    if not built.has_columns or built.clear_two_columns:
        return [Segment("lines", atoms)], tables, 0
    rows = _rows(built.lines)
    heights = [line.bbox[3] - line.bbox[1] for line in built.lines]
    median_height = statistics.median(heights) if heights else 0.0
    gap_limit = max(COLUMN_GAP_MIN, median_height * COLUMN_GAP_HEIGHTS)
    consumed: set[int] = set()
    tables, moved = _extend_ruled(rows, ruled, median_height, consumed, page_index, page_height)
    runs = _runs(rows, consumed, gap_limit)
    segments: list[Segment] = []
    pending: list[Atom] = []
    start_of = {run[0]: run for run in runs}
    i = 0
    while i < len(rows):
        if i in consumed:
            i += 1
            continue
        run = start_of.get(i)
        if run is None:
            pending.extend(rows[i].atoms)
            i += 1
            continue
        segment = _decide(rows, run, consumed, median_height, gap_limit, rtl, page_index, repairs_on)
        if segment.kind == "lines":
            pending.extend(segment.atoms)
        else:
            if pending:
                segments.append(Segment("lines", pending))
                pending = []
            segments.append(segment)
        i = run[-1] + 1
    if pending:
        segments.append(Segment("lines", pending))
    return segments, tables, moved


def _rows(lines: list[BuiltLine]) -> list[_Row]:
    by_row: dict[int, list[BuiltLine]] = {}
    for line in lines:
        by_row.setdefault(line.row, []).append(line)
    out = [_Row(sorted(group, key=lambda l: l.bbox[0])) for _, group in sorted(by_row.items())]
    return sorted(out, key=lambda r: r.top)


# ---------------------------------------------------------------- rows that continue a ruled table


def _fits(row: _Row, xs: list[float]) -> list[tuple[int, int]] | None:
    """The table columns each of the row's lines covers (first, last), or None when a line ends
    between column boundaries. A line over several columns is a spanned cell."""
    out = []
    for line in row.lines:
        first = next((c for c in range(len(xs) - 1) if xs[c] - EDGE <= line.bbox[0] < xs[c + 1]), None)
        last = next((c for c in range(len(xs) - 1) if xs[c] < line.bbox[2] <= xs[c + 1] + EDGE), None)
        if first is None or last is None or last < first:
            return None
        out.append((first, last))
    return out


def _extend_ruled(rows, ruled, median_height, consumed, page_index, page_height=None) -> tuple[list[Block], int]:
    near = NEAR * median_height + 2.0
    # The page's bottom margin band (as furniture.BAND): a row there below a table is a footer, not a table row,
    # unless a ruled table resumes after it.
    footer_top = page_height * (1 - FOOTER_BAND) if page_height else None
    order = sorted(range(len(ruled)), key=lambda k: ruled[k][0].bbox[1])
    blocks = [ruled[k][0] for k in order]
    grids = [ruled[k][1] for k in order]
    moved = 0
    merged_into: dict[int, int] = {}
    for t, (block, xs) in enumerate(zip(blocks, grids)):
        if t in merged_into or len(xs) < 3:
            continue
        bottom = block.bbox[3]
        taken: list[int] = []
        for i, row in enumerate(rows):
            if i in consumed or row.top < bottom - 2.0:
                continue
            spans = _fits(row, xs)
            if row.top - bottom > near or spans is None or spans == [(0, len(xs) - 2)]:
                break  # outside the columns, or one line across the whole table: a paragraph
            taken.append(i)
            bottom = row.bottom
        # Bridge to a ruled table with the same columns that resumes right below.
        follower = next(
            (
                u
                for u in range(t + 1, len(blocks))
                if u not in merged_into
                and len(grids[u]) == len(xs)
                and all(abs(a - b) <= EDGE for a, b in zip(grids[u], xs))
                and -2.0 <= blocks[u].bbox[1] - bottom <= near
            ),
            None,
        )
        if follower is None and footer_top is not None:
            while taken and rows[taken[-1]].top >= footer_top:
                taken.pop()  # a running footer under the table: furniture decides it, not this table
        if follower is None:
            while taken and not rows[taken[-1]].multi:
                taken.pop()
            if not any(rows[i].multi for i in taken):
                continue
        if not taken and follower is None:
            continue
        if block._cell_repairs is None:
            block._cell_repairs = CellRepairs()
        new_rows = _ruled_rows([rows[i] for i in taken], xs, block.direction == "rtl", median_height, block._cell_repairs)
        block.cells = list(block.cells or []) + new_rows
        moved += sum(_ink(rows[i].atoms) for i in taken)
        consumed.update(taken)
        if taken:
            block.bbox = [block.bbox[0], block.bbox[1], block.bbox[2], max(block.bbox[3], max(rows[i].bottom for i in taken))]
        if follower is not None:
            other = blocks[follower]
            block.cells = block.cells + list(other.cells or [])
            block.bbox = [min(block.bbox[0], other.bbox[0]), block.bbox[1], max(block.bbox[2], other.bbox[2]), max(block.bbox[3], other.bbox[3])]
            block.flags = sorted(set(block.flags) | set(other.flags))
            if other._cell_repairs is not None:
                mine = block._cell_repairs
                mine.bullets.update(other._cell_repairs.bullets)
                mine.eth += other._cell_repairs.eth
                mine.pua.update(other._cell_repairs.pua)
            merged_into[follower] = t
        if "table_rows_unruled" not in block.flags:
            block.flags.append("table_rows_unruled")
    return [b for u, b in enumerate(blocks) if u not in merged_into], moved


def _ruled_rows(rows: list[_Row], xs: list[float], rtl: bool, median_height: float, ledger: CellRepairs) -> list[list[Cell]]:
    """Table rows from visual rows that continue a ruled table, padded to its columns. A line
    over several columns gives a spanned cell; a lone line close to the row above continues it."""
    columns = len(xs) - 1
    spans = [_fits(r, xs) or [] for r in rows]
    grouped = _table_rows(rows, [[a for a, _ in s] for s in spans])
    out = []
    for group in grouped:
        pieces = [(span, line) for row, _ in group for span, line in zip(_fits(row, xs) or [], row.lines)]
        merged: list[list] = []  # [first, last, atoms]
        for (first, last), line in sorted(pieces, key=lambda p: p[0]):
            if merged and first <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], last)
                merged[-1][2].extend(line.atoms)
            else:
                merged.append([first, last, list(line.atoms)])
        cells: list[Cell] = []
        c = 0
        for first, last, atoms in merged:
            cells.extend(Cell("") for _ in range(first - c))
            cell_lines: list = []
            text = ledger.text(atoms, rtl, cell_lines)
            cells.append(Cell(text, colspan=last - first + 1, _lines=cell_lines))
            c = last + 1
        cells.extend(Cell("") for _ in range(columns - c))
        out.append(list(reversed(cells)) if rtl else cells)
    return out


# ---------------------------------------------------------------- runs of side-by-side rows


def _gutters(rows: list[_Row], gap_limit: float) -> list[tuple[float, float]]:
    """Strips free of ink in every row, between the run's outer edges."""
    lines = [line for row in rows for line in row.lines]
    left = min(line.bbox[0] for line in lines)
    right = max(line.bbox[2] for line in lines)
    spans = sorted((line.bbox[0], line.bbox[2]) for line in lines)
    out = []
    reach = left
    for x0, x1 in spans:
        if x0 - reach >= GUTTER_SHARE * gap_limit:
            out.append((reach, x0))
        reach = max(reach, x1)
    return [g for g in out if g[0] > left and g[1] < right]


def _runs(rows: list[_Row], consumed: set[int], gap_limit: float) -> list[list[int]]:
    runs: list[list[int]] = []
    i = 0
    while i < len(rows):
        if i in consumed or not rows[i].multi:
            i += 1
            continue
        run = [i]
        j = i + 1
        kept = len(_gutters([rows[i]], gap_limit))
        while j < len(rows) and j not in consumed:
            gutters = _gutters([rows[k] for k in run + [j]], gap_limit)
            if not gutters or len(gutters) < kept or len(rows[j].lines) > len(gutters) + 1:
                # A row that removes one of the run's gutters, or has more pieces than the run has
                # bands, starts a run of its own.
                break
            run.append(j)
            kept = len(gutters)
            j += 1
        while len(run) > 1 and not rows[run[-1]].multi:
            run.pop()
        runs.append(run)
        i = run[-1] + 1
    return runs


def _band(line: BuiltLine, gutters: list[tuple[float, float]]) -> int:
    centre = (line.bbox[0] + line.bbox[2]) / 2
    return sum(1 for g in gutters if (g[0] + g[1]) / 2 < centre)


def _decide(rows, run, consumed, median_height, gap_limit, rtl, page_index, repairs_on=True) -> Segment:
    members = [rows[i] for i in run]
    atoms = [a for row in members for a in row.atoms]
    gutters = _gutters(members, gap_limit)
    unresolved = Segment("lines", atoms)
    if not gutters:
        return unresolved
    bands_of = [[_band(line, gutters) for line in row.lines] for row in members]
    if any(len(set(b)) != len(b) for b in bands_of):
        return unresolved  # two pieces of one row in one band: the bands are not stable
    first = len(gutters) if rtl else 0
    labels = [line.text.strip() for row, bands in zip(members, bands_of) for line, b in zip(row.lines, bands) if b == first]
    if labels and all(LABEL.match(t) for t in labels):
        # Labels in the margin: row by row keeps each label before its text, whatever follows.
        return Segment("rows", atoms, bands=[row.atoms for row in members])
    near = NEAR * median_height + 2.0
    gaps = sorted(members[n + 1].top - members[n].bottom for n in range(len(members) - 1))
    pitch = gaps[len(gaps) // 2] if gaps else 0.5 * median_height
    for k in (run[0] - 1, run[-1] + 1):
        if 0 <= k < len(rows) and k not in consumed:
            other = rows[k]
            gap = members[0].top - other.bottom if k < run[0] else other.top - members[-1].bottom
            if gap <= near and _gutters(members + [other], gap_limit) and len(other.lines) == 1:
                adjacent = members[0] if k < run[0] else members[-1]
                if gap <= pitch + PITCH_SLACK * median_height and _lines_up(other.lines[0], adjacent, gutters, rtl, median_height):
                    return unresolved  # a neighbouring line could belong to one of the bands
    count = len(gutters) + 1
    if len(members) <= 2:
        # Each row is built alone, so two stacked rows are never re-read column by column.
        return Segment("rows", atoms, bands=[row.atoms for row in members])
    if count <= MAX_FLOW_BANDS and len(members) >= MIN_FLOW_ROWS and _prose(members, bands_of, gutters, count):
        order = range(count - 1, -1, -1) if rtl else range(count)
        bands = [[a for row, bands in zip(members, bands_of) for line, b in zip(row.lines, bands) if b == band for a in line.atoms] for band in order]
        return Segment("flow", atoms, bands=bands)
    if len(members) >= MIN_TABLE_ROWS and sum(1 for row in members if row.multi) >= 2:
        if _text_columns(members, bands_of, count):
            return unresolved  # prose beside prose or a list: columns of text, whose order is not known
        groups = _table_rows(members, bands_of)
        cells = []
        placed = 0
        ledger = CellRepairs(repairs_on)
        for group in groups:
            texts = _cell_texts(group, count, rtl, ledger)
            cells.append([Cell(text, _lines=lines) for text, lines in (reversed(texts) if rtl else texts)])
        if sum(1 for row in cells for cell in row if not cell.text.strip()) > SPARSE * sum(len(row) for row in cells):
            return unresolved  # mostly empty: labels scattered over a chart, not a table's records
        placed = _ink(atoms)
        bbox = [
            min(line.bbox[0] for row in members for line in row.lines),
            members[0].top,
            max(line.bbox[2] for row in members for line in row.lines),
            members[-1].bottom,
        ]
        block = Block(
            kind="table",
            cells=cells,
            page=page_index,
            bbox=bbox,
            source="text_layer",
            tool=TOOL,
            direction="rtl" if rtl else "ltr",
            flags=["unruled_table"],
            _cell_repairs=ledger,
        )
        return Segment("table", atoms, block=block, chars=placed)
    return unresolved


def _text_columns(members: list[_Row], bands_of: list[list[int]], count: int) -> bool:
    """Whether a run's bands are columns of text rather than a table's columns (see PROSE_WORDS)."""
    stats = []
    for band in range(count):
        texts = [line.text for row, bands in zip(members, bands_of) for line, b in zip(row.lines, bands) if b == band and line.text.strip()]
        if not texts:
            continue
        words = [len(t.split()) for t in texts]
        stats.append(
            (
                sum(w >= PROSE_WORDS for w in words) / len(texts) >= PROSE_SHARE,
                sum(words) / len(texts) >= TEXT_WORDS,
                sum(bool(MARKER.match(t)) for t in texts) / len(texts) >= LIST_SHARE,
            )
        )
    for n, (prose, _, listed) in enumerate(stats):
        others = stats[:n] + stats[n + 1 :]
        if prose and any(text or other_list for _, text, other_list in others):
            return True
        if listed and any(other_list for _, _, other_list in others):
            return True
    return False


def _lines_up(line: BuiltLine, row: _Row, gutters, rtl: bool, median_height: float) -> bool:
    """Whether a line lines up with the piece of the row in its band, as a line of that column would:
    the same reading-side edge (right in RTL), or the same centre. With no piece in that band, it may."""
    band = _band(line, gutters)
    piece = next((other for other in row.lines if _band(other, gutters) == band), None)
    if piece is None:
        return True
    tolerance = ALIGN * median_height
    edge = abs(line.bbox[2] - piece.bbox[2]) if rtl else abs(line.bbox[0] - piece.bbox[0])
    centre = abs((line.bbox[0] + line.bbox[2]) - (piece.bbox[0] + piece.bbox[2])) / 2
    return edge <= tolerance or centre <= tolerance


def _prose(members, bands_of, gutters, count) -> bool:
    left = min(line.bbox[0] for row in members for line in row.lines)
    right = max(line.bbox[2] for row in members for line in row.lines)
    edges = [left] + [x for g in gutters for x in g] + [right]
    widths = [edges[2 * b + 1] - edges[2 * b] for b in range(count)]
    if min(widths) < FLOW_BAND * (right - left):
        return False
    for band in range(count):
        lines = [line for row, bands in zip(members, bands_of) for line, b in zip(row.lines, bands) if b == band]
        if len(lines) < 3 or widths[band] <= 0:
            return False
        full = sum(1 for line in lines if line.bbox[2] - line.bbox[0] >= FLOW_FILL * widths[band])
        if full < FLOW_LINES * len(lines):
            return False
    return True


def _table_rows(members: list[_Row], bands_of: list[list[int]]) -> list[list[tuple[_Row, list[int]]]]:
    """Visual rows grouped into table rows: a lone line close to the row above continues its cell."""
    gaps = [members[k].top - members[k - 1].bottom for k in range(1, len(members)) if members[k].multi]
    usual = statistics.median(gaps) if len(gaps) >= 2 else None
    groups: list[list[tuple[_Row, list[int]]]] = []
    for k, (row, bands) in enumerate(zip(members, bands_of)):
        if groups and usual is not None and not row.multi and usual > 0:
            gap = row.top - members[k - 1].bottom
            above = {b for _, bs in groups[-1] for b in bs}
            if gap < CONTINUATION * usual and bands[0] in above:
                groups[-1].append((row, bands))
                continue
        groups.append([(row, bands)])
    return groups


def _cell_texts(group: list[tuple[_Row, list[int]]], count: int, rtl: bool, ledger: CellRepairs) -> list[tuple[str, list]]:
    """Cell texts of one table row, left to right, each with its lines' [text, bbox]."""
    per_band: list[list[Atom]] = [[] for _ in range(count)]
    for row, bands in group:
        for line, band in zip(row.lines, bands):
            if 0 <= band < count:
                per_band[band].extend(line.atoms)
    out = []
    for atoms in per_band:
        lines: list = []
        out.append((ledger.text(atoms, rtl, lines) if atoms else "", lines))
    return out
