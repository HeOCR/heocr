"""Ruled tables in PDF text-layer pages (D3b).

**Rulings.** Rulings come from the page's vector paths, after the object's matrix is applied.
Every straight piece of a stroked path is a ruling. A filled path that is not stroked is read
by subpath: a thin one (a border drawn as a filled rectangle, as Word does) gives one ruling
along its centre, and a wide one is **shading**, never a ruling. Word shades each cell, and
each line of text inside it, with its own rectangle, so shading edges taken as rulings split
a cell of two text lines into two rows and gave columns that were not there. A grid drawn as
one path, as thin filled rectangles, or as stroked cell boxes all yield the same lines, and
collinear pieces are merged.

**The grid.** Intersecting horizontal and vertical lines form a table. Its distinct line
positions give rows and columns. A cell boundary exists where a ruling covers most of that
edge, and cells with a missing boundary between them merge into spans. A merge that is not
a rectangle is flagged ``table_grid_unresolved``, and that table's text stays as lines.

**Cell text from the page.** Each cell takes the text-layer characters whose centres fall
inside it, built into lines by the same RTL line building. No word is invented, and no
detector is a text source.

**Direction per document** (the volunteer's ``scripts/clean.py`` rule 8): in a Hebrew
document every table is RTL, an English one included, so a row's first cell is its rightmost.

**Padding.** Every row is padded to the full grid. A spanned cell is followed by nothing, and
the positions a vertical merge covers in later rows hold empty ``merged_above`` cells, so no
cell shifts column.
"""

from __future__ import annotations

import ctypes
from collections import Counter
from dataclasses import dataclass, field

from heocr import repairs
from heocr.contract import Block, Cell
from heocr.pdf.lines import Atom, build_lines, carry_latin_context

TOOL = "heocr.pdf"
STRAIGHT = 0.6  # pt: a piece this close to horizontal or vertical is a ruling piece
MIN_PIECE = 4.0  # pt: shorter pieces (dots, glyph-like paths) are ignored
SNAP = 2.5  # pt: rulings this close are the same line (double lines, thin filled rectangles)
JOIN = 3.0  # pt: collinear pieces closer than this are one line
COVER = 0.6  # a boundary exists when a ruling covers this share of the cell edge
MIN_GAP = 6.0  # pt: grid lines closer than this are one line (cells drawn as separate boxes double each border)
FULL_ROW = 0.8  # row by row: a band boundary is a ruling across this share of the table's width
INSIDE = 1.0  # pt: a character belongs to a cell when its centre is this far inside or more
THIN_FILL = 4.0  # pt: a filled, unstroked subpath at most this thick is a border; a thicker one is shading


@dataclass
class Rule:
    pos: float  # y for horizontal, x for vertical (top-left origin)
    start: float
    end: float


@dataclass
class Table:
    bbox: list[float]
    rows: list[list[dict]]  # per row, cells: {"box": [...], "colspan", "rowspan", "placeholder"}
    flags: list[str] = field(default_factory=list)
    xs: list[float] = field(default_factory=list)  # the grid's column positions, left to right


@dataclass
class CellRepairs:
    """Repairs made in one table's cells, logged on the table block by the reader.

    A cell's text gets the same line repairs as a paragraph read from the layer (the
    symbol-font bullet), and its characters' own repairs (ð per font, private-use codes learned
    by OCR) are counted, so a table logs what a paragraph would have.
    """

    on: bool = True
    bullets: Counter = field(default_factory=Counter)
    eth: int = 0
    pua: Counter = field(default_factory=Counter)  # (font, letter)
    shifted: Counter = field(default_factory=Counter)  # (font, letter): cp1255_shifted_font

    def text(self, atoms: list[Atom], rtl: bool, lines: list | None = None) -> str:
        """The cell's text; with ``lines``, each line's [text, bbox] is appended to it (number checks)."""
        texts = []
        built = build_lines(atoms, rtl=rtl).lines
        carry_latin_context(built)  # a cell is one paragraph: a reference may wrap in a narrow column
        for line in built:
            text = line.text
            if self.on:
                text, changes = repairs.symbol_font_bullet(text)
                self.bullets.update(changes)
            texts.append(text)
            if lines is not None:
                lines.append([text, list(line.bbox)])
        for atom in atoms:
            if atom.repaired == repairs.LATIN1_ETH_NUN.rule_id:
                self.eth += 1
            elif atom.repaired == repairs.FONT_PRIVATE_USE_BY_OCR.rule_id:
                self.pua[(atom.font, atom.text)] += 1
            elif atom.repaired == repairs.CP1255_SHIFTED_FONT.rule_id:
                self.shifted[(atom.font, atom.text)] += 1
        return "\n".join(texts)


def rulings(page) -> tuple[list[Rule], list[Rule]]:
    """Horizontal and vertical ruling lines of a page, in points, top-left origin."""
    import pypdfium2.raw as raw

    from heocr.pdf.frame import frame_of

    frame = frame_of(page)
    height = frame.height  # points are taken into the page's frame, then given back y-up for the flips below
    horizontal: list[Rule] = []
    vertical: list[Rule] = []
    for obj in page.get_objects(max_depth=8):
        if obj.type != raw.FPDF_PAGEOBJ_PATH:
            continue
        matrix = raw.FS_MATRIX()
        if not raw.FPDFPageObj_GetMatrix(obj.raw, ctypes.byref(matrix)):
            continue
        fill, stroke = ctypes.c_int(), ctypes.c_int()
        if not raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(fill), ctypes.byref(stroke)):
            fill.value, stroke.value = 0, 1
        fill_only = bool(fill.value) and not stroke.value
        a, b, c, d, e, f = matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f
        points: list[tuple[float, float]] = []
        subpath: list[tuple[float, float]] = []
        start: tuple[float, float] | None = None
        count = raw.FPDFPath_CountSegments(obj.raw)
        for i in range(max(count, 0)):
            segment = raw.FPDFPath_GetPathSegment(obj.raw, i)
            x, y = ctypes.c_float(), ctypes.c_float()
            raw.FPDFPathSegment_GetPoint(segment, ctypes.byref(x), ctypes.byref(y))
            fx, fy = frame.point(a * x.value + c * y.value + e, b * x.value + d * y.value + f)
            px, py = fx, height - fy
            kind = raw.FPDFPathSegment_GetType(segment)
            if kind == raw.FPDF_SEGMENT_MOVETO:
                if fill_only:
                    _filled(subpath, height, horizontal, vertical)
                start = (px, py)
                points = [start]
                subpath = [start]
                continue
            subpath.append((px, py))
            if fill_only:
                continue
            if points:
                _piece(points[-1], (px, py), height, horizontal, vertical, kind == raw.FPDF_SEGMENT_LINETO)
            points.append((px, py))
            if raw.FPDFPathSegment_GetClose(segment) and start is not None:
                _piece((px, py), start, height, horizontal, vertical, True)
        if fill_only:
            _filled(subpath, height, horizontal, vertical)
    return _merge(horizontal), _merge(vertical)


def _filled(points, height, horizontal, vertical) -> None:
    """A filled, unstroked subpath: a ruling along its centre when thin, nothing when it is shading."""
    if len(points) < 2:
        return
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = height - max(p[1] for p in points), height - min(p[1] for p in points)
    w, h = x1 - x0, y1 - y0
    if h <= THIN_FILL and w >= MIN_PIECE and w >= h:
        horizontal.append(Rule((y0 + y1) / 2, x0, x1))
    elif w <= THIN_FILL and h >= MIN_PIECE:
        vertical.append(Rule((x0 + x1) / 2, y0, y1))


def _piece(p, q, height, horizontal, vertical, straight: bool) -> None:
    if not straight:
        return
    (x0, y0), (x1, y1) = p, q
    y0, y1 = height - y0, height - y1
    if abs(y1 - y0) <= STRAIGHT and abs(x1 - x0) >= MIN_PIECE:
        horizontal.append(Rule((y0 + y1) / 2, min(x0, x1), max(x0, x1)))
    elif abs(x1 - x0) <= STRAIGHT and abs(y1 - y0) >= MIN_PIECE:
        vertical.append(Rule((x0 + x1) / 2, min(y0, y1), max(y0, y1)))


def _merge(rules: list[Rule]) -> list[Rule]:
    """Snap rulings to shared positions and join collinear pieces."""
    out: list[Rule] = []
    for rule in sorted(rules, key=lambda r: (r.pos, r.start)):
        for other in reversed(out[-50:]):
            if abs(other.pos - rule.pos) <= SNAP and rule.start <= other.end + JOIN and rule.end >= other.start - JOIN:
                other.start, other.end = min(other.start, rule.start), max(other.end, rule.end)
                break
        else:
            out.append(Rule(rule.pos, rule.start, rule.end))
    return out


def _positions(values: list[float]) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > MIN_GAP:
            out.append(v)
    return out


def _assign(rules: list[Rule], positions: list[float]) -> list[Rule]:
    """Each ruling moved onto its nearest grid position (within MIN_GAP), so it counts for that line only."""
    out = []
    for r in rules:
        nearest = min(positions, key=lambda p: abs(p - r.pos))
        if abs(nearest - r.pos) <= MIN_GAP:
            out.append(Rule(nearest, r.start, r.end))
    return out


def _covered(rules: list[Rule], pos: float, lo: float, hi: float) -> bool:
    need = COVER * (hi - lo)
    covered = sum(max(0.0, min(r.end, hi) - max(r.start, lo)) for r in rules if abs(r.pos - pos) <= 0.01)
    return covered >= need


def find_tables(horizontal: list[Rule], vertical: list[Rule], *, require_closed: bool = False) -> list[Table]:
    """Tables formed by intersecting rulings, optionally requiring a supported perimeter."""
    lines = [("h", r) for r in horizontal] + [("v", r) for r in vertical]
    parent = list(range(len(lines)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, (ki, ri) in enumerate(lines):
        if ki != "h":
            continue
        for j, (kj, rj) in enumerate(lines):
            if kj == "v" and rj.start - SNAP <= ri.pos <= rj.end + SNAP and ri.start - SNAP <= rj.pos <= ri.end + SNAP:
                parent[root(i)] = root(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(lines)):
        groups.setdefault(root(i), []).append(i)
    tables = []
    for members in groups.values():
        hs = [lines[i][1] for i in members if lines[i][0] == "h"]
        vs = [lines[i][1] for i in members if lines[i][0] == "v"]
        if len(hs) < 2 or len(vs) < 2:
            continue
        opened = [] if require_closed else _open_sides(hs, vs)
        table = _grid(hs, vs + opened, require_closed=require_closed)
        if table is not None:
            if opened and table.rows:
                table.flags.append("table_open_sides_closed")
            tables.append(table)
    return tables


OPEN_SIDE = 8.0  # pt: a grid's top and bottom rulings running this far past its outer vertical leave an open column


def _open_sides(hs: list[Rule], vs: list[Rule]) -> list[Rule]:
    """Closing verticals for a grid drawn with open sides (F10): when its top and bottom rulings both run at least
    OPEN_SIDE past the outermost vertical on a side, and end together (within SNAP), the column they bound holds
    cells too. A vertical is added at their end, from the top ruling to the bottom one."""
    top, bottom = min(hs, key=lambda r: r.pos), max(hs, key=lambda r: r.pos)
    left, right = min(v.pos for v in vs), max(v.pos for v in vs)
    added = []
    for end_of, outer, sign in ((lambda r: r.start, left, -1), (lambda r: r.end, right, 1)):
        ends = [end_of(top), end_of(bottom)]
        if all(sign * (e - outer) >= OPEN_SIDE for e in ends) and abs(ends[0] - ends[1]) <= SNAP:
            x = (ends[0] + ends[1]) / 2
            added.append(Rule(x, top.pos, bottom.pos))
    return added


def _grid(hs: list[Rule], vs: list[Rule], *, require_closed: bool) -> Table | None:
    ys = _positions([r.pos for r in hs])
    xs = _positions([r.pos for r in vs])
    if len(ys) < 2 or len(xs) < 2:
        return None
    hs, vs = _assign(hs, ys), _assign(vs, xs)
    if require_closed:
        # A short outer stroke can attach to a real grid and create an extra band.
        # Count joined segments on each outer edge once, using the same support as cell edges.
        outer_h, outer_v = _merge(hs), _merge(vs)
        if not (all(_covered(outer_h, y, xs[0], xs[-1]) for y in (ys[0], ys[-1]))
                and all(_covered(outer_v, x, ys[0], ys[-1]) for x in (xs[0], xs[-1]))):
            return None
    rows, cols = len(ys) - 1, len(xs) - 1
    parent = {(r, c): (r, c) for r in range(rows) for c in range(cols)}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for r in range(rows):
        for c in range(cols):
            if c + 1 < cols and not _covered(vs, xs[c + 1], ys[r], ys[r + 1]):
                parent[root((r, c))] = root((r, c + 1))
            if r + 1 < rows and not _covered(hs, ys[r + 1], xs[c], xs[c + 1]):
                parent[root((r, c))] = root((r + 1, c))
    merged: dict[tuple, list[tuple[int, int]]] = {}
    for key in parent:
        merged.setdefault(root(key), []).append(key)
    flags: list[str] = []
    spans = []
    for cells in merged.values():
        r0, r1 = min(r for r, _ in cells), max(r for r, _ in cells)
        c0, c1 = min(c for _, c in cells), max(c for _, c in cells)
        if len(cells) != (r1 - r0 + 1) * (c1 - c0 + 1):
            flags.append("table_grid_unresolved")
        spans.append((r0, r1, c0, c1))
    if "table_grid_unresolved" in flags:
        # Word tables often give each row its own column boundaries; a global grid then needs
        # merges that are not rectangles. Read the rows one by one instead.
        return _rows_independent(hs, vs, xs, ys)
    if len(spans) < 2 or cols < 2 and all(c1 == c0 for _, _, c0, c1 in spans):
        return Table([xs[0], ys[0], xs[-1], ys[-1]], [], ["table_single_column"])
    by_start = {(r0, c0): (r0, r1, c0, c1) for r0, r1, c0, c1 in spans}
    covered = {(r, c): (r0, c0) for r0, r1, c0, c1 in spans for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
    grid_rows = []
    for r in range(rows):
        row = []
        for c in range(cols):
            origin = covered[(r, c)]
            r0, r1, c0, c1 = by_start[origin]
            if origin == (r, c):
                row.append({"box": [xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1]], "colspan": c1 - c0 + 1, "rowspan": r1 - r0 + 1, "placeholder": False, "col": c})
            elif r > r0 and c == c0:
                row.append({"box": None, "colspan": c1 - c0 + 1, "rowspan": 1, "placeholder": True, "col": c})
        grid_rows.append(row)
    return Table([xs[0], ys[0], xs[-1], ys[-1]], grid_rows, flags, list(xs))


def _rows_independent(hs: list[Rule], vs: list[Rule], xs: list[float], ys: list[float]) -> Table:
    """A table read row by row: each band between full-width rulings gets its own columns, and
    there are no row spans. When even that fails, the grid stays unresolved."""
    left, right = xs[0], xs[-1]
    width = right - left
    full = [y for y in ys if sum(max(0.0, min(r.end, right) - max(r.start, left)) for r in hs if abs(r.pos - y) <= 0.01) >= FULL_ROW * width]
    bands = list(zip(full, full[1:]))
    rows = []
    for top, bottom in bands:
        cuts = [x for x in xs if _covered(vs, x, top, bottom)]
        if len(cuts) < 2 or abs(cuts[0] - left) > MIN_GAP or abs(cuts[-1] - right) > MIN_GAP:
            return Table([left, ys[0], right, ys[-1]], [], ["table_grid_unresolved"])
        row = []
        for c, (x0, x1) in enumerate(zip(cuts, cuts[1:])):
            span = sum(1 for x in xs if x0 - MIN_GAP <= x < x1 - MIN_GAP)
            row.append({"box": [x0, top, x1, bottom], "colspan": max(span, 1), "rowspan": 1, "placeholder": False, "col": xs.index(min(xs, key=lambda v: abs(v - x0)))})
        rows.append(row)
    if len(rows) < 1 or max(len(r) for r in rows) < 2:
        return Table([left, ys[0], right, ys[-1]], [], ["table_grid_unresolved"])
    return Table([left, ys[0], right, ys[-1]], rows, ["table_rows_independent"], list(xs))


def table_blocks(page_index: int, atoms: list[Atom], tables: list[Table], rtl: bool, repairs_on: bool = True) -> tuple[list[Block], list[Atom], int, int, list[list[float]]]:
    """(table blocks, the atoms left for lines, characters placed in cells, grids rejected, each block's column positions)."""
    remaining = list(atoms)
    blocks: list[Block] = []
    grids: list[list[float]] = []
    rejected: list[Table] = []
    placed = 0
    for table in tables:
        if not table.rows:
            continue
        inside = [a for a in remaining if _within(a, table.bbox, 0.0)]
        if not any(not a.is_space for a in inside):
            continue  # a frame or a drawing, not a table of this layer's text
        proposed = "tableformer_proposed" in table.flags
        if not proposed and _crosses_text(table, inside):
            # Real table rulings never cut through characters: these are underlines, form lines
            # or a column rule over running text. The text stays as lines.
            rejected.append(table)
            continue
        points: dict[int, tuple[float, float]] = {}
        if proposed:
            # A learned grid is cut where the fewest characters are; a word it still cuts goes whole to one cell.
            points = _whole_words(inside, [cell["box"] for row in table.rows for cell in row if cell["box"]])
            if points:
                table.flags.append("table_words_kept_whole")
        taken: set[int] = set()
        ledger = CellRepairs(repairs_on)
        cells_out: list[list[Cell]] = []
        for row in table.rows:
            ordered = sorted(row, key=lambda cell: -cell["col"] if rtl else cell["col"])
            out_row: list[Cell] = []
            for cell in ordered:
                if cell["placeholder"]:
                    out_row.append(Cell("", colspan=cell["colspan"], flags=["merged_above"]))
                    continue
                mine = [a for a in inside if id(a) not in taken and _within_at(a, points.get(id(a)), cell["box"], INSIDE)]
                taken.update(id(a) for a in mine)
                cell_lines: list = []
                text = ledger.text(mine, rtl, cell_lines)
                placed += sum(1 for a in mine if not a.is_space)
                narrow = cell["box"][2] - cell["box"][0] < NARROW_CELL and cell["rowspan"] == 1 and not mine
                if narrow and out_row and "merged_above" not in out_row[-1].flags:
                    # An empty sliver between doubled borders: folded into its neighbour's span.
                    out_row[-1].colspan += cell["colspan"]
                    continue
                out_row.append(Cell(text, colspan=cell["colspan"], rowspan=cell["rowspan"], flags=["narrow"] if narrow else [], _lines=cell_lines))
            # A leading sliver joins the cell after it.
            while len(out_row) > 1 and "narrow" in out_row[0].flags:
                sliver = out_row.pop(0)
                out_row[0].colspan += sliver.colspan
            for cell_out in out_row:
                if "narrow" in cell_out.flags:
                    cell_out.flags.remove("narrow")
            cells_out.append(out_row)
        remaining = [a for a in remaining if id(a) not in taken]
        blocks.append(
            Block(
                kind="table",
                cells=cells_out,
                page=page_index,
                bbox=list(table.bbox),
                source="text_layer",
                tool=TOOL,
                direction="rtl" if rtl else "ltr",
                flags=[*([] if "tableformer_proposed" in table.flags else ["ruled_table"]), *table.flags],
                _cell_repairs=ledger,
            )
        )
        grids.append(list(table.xs))
    return blocks, remaining, placed, len(rejected), grids


NARROW_CELL = 12.0  # pt: an empty cell narrower than this is a sliver between doubled borders
CROSSING_SHARE = 0.02  # a grid whose interior boundaries cut through more of its characters is not a table


def _crosses_text(table: Table, atoms: list[Atom]) -> bool:
    ink = [a for a in atoms if not a.is_space]
    crossed = 0
    for row in table.rows:
        for cell in row:
            box = cell["box"]
            if box is None:
                continue
            for a in ink:
                if box[1] <= a.cy <= box[3] and (a.x0 + 0.5 < box[0] < a.x1 - 0.5 or a.x0 + 0.5 < box[2] < a.x1 - 0.5):
                    crossed += 1
    return crossed > CROSSING_SHARE * max(len(ink), 1)


WORD_GAP = 0.15  # letters closer than this share of their height, on one line, are one word


def _whole_words(atoms: list[Atom], boxes: list[list[float]]) -> dict[int, tuple[float, float]]:
    """For each word whose letters fall in more than one cell, or partly in none (a letter on a column line): every
    letter's point moved to one letter of the word in the cell that holds most of it (ties: the leftmost). Only
    letters: a number's digits and marks keep their own places, since layers often store a decimal's parts apart."""
    lines: list[list[Atom]] = []
    for a in sorted((a for a in atoms if not a.is_space and a.text[:1].isalpha()), key=lambda a: a.cy):
        if lines and abs(a.cy - lines[-1][-1].cy) < 0.5 * max(a.y1 - a.y0, 1.0):
            lines[-1].append(a)
        else:
            lines.append([a])
    words: list[list[Atom]] = []
    for line in lines:
        line.sort(key=lambda a: a.x0)
        for i, a in enumerate(line):
            gap = a.x0 - line[i - 1].x1 if i else None
            if gap is not None and -0.5 * max(a.y1 - a.y0, 1.0) <= gap <= WORD_GAP * max(a.y1 - a.y0, 1.0):
                words[-1].append(a)
            else:
                words.append([a])
    points: dict[int, tuple[float, float]] = {}
    for word in words:
        homes: dict[int, list[Atom]] = {}
        housed = 0
        for a in word:
            for i, box in enumerate(boxes):
                if _within(a, box, INSIDE):
                    homes.setdefault(i, []).append(a)
                    housed += 1
                    break
        if not homes or (len(homes) == 1 and housed == len(word)):
            continue
        anchor = max(homes.values(), key=len)[0]
        for a in word:
            points[id(a)] = (anchor.cx, anchor.cy)
    return points


def _within_at(atom: Atom, point, box, margin: float) -> bool:
    if point is None:
        return _within(atom, box, margin)
    return box[0] + margin <= point[0] <= box[2] - margin and box[1] + margin <= point[1] <= box[3] - margin


def _within(atom: Atom, box, margin: float) -> bool:
    return box[0] + margin <= atom.cx <= box[2] - margin and box[1] + margin <= atom.cy <= box[3] - margin
