"""Right-to-left line building from character boxes: our own reading order for PDF text layers.

Ported code (see NOTICE), rewritten without PyMuPDF: row clustering by baseline, column-sized
gaps, the stable two-column gutter test, the direction test, the tight-join spacing rules, the
``selection_safe`` rule, and the RTL line with its LTR sub-runs kept in visual order.

The original worked on words that PyMuPDF had already formed. Here the atoms are characters from
pypdfium2. The inverse bidi pass therefore runs on characters, so mixed words such as
``ב-2026`` come out in logical order.

The visual order of a line does not always determine its logical order. For example,
``סעיף 3.1 ABC`` and ``סעיף ABC 3.1`` display identically. Such lines keep the
convention of the ported code, which is to keep a mixed Latin and number run in visual order, and they are
flagged ``bidi_ambiguous_ltr_run``.
"""

from __future__ import annotations

import statistics
import unicodedata
from dataclasses import dataclass, field

# A gap wider than this share of the loose box height (the font's ascent to descent, about
# 1.1-1.2 em), with no space character in it, is a word break. The reported font size is not used:
# many producers set it to 1 and scale the text matrix instead.
WORD_GAP_EM = 0.15
# Rows cluster within 0.42 of the median height; a column gap is max(24 pt, 2.5 heights).
ROW_TOLERANCE = 0.42
COLUMN_GAP_MIN = 24.0
COLUMN_GAP_HEIGHTS = 2.5
# A punctuation join counts as tight within 0.35 of the smaller height.
TIGHT_JOIN = 0.35
# Tight punctuation hugs the word on one side only when its gap on the other side is wider by more
# than this factor. A mark with equal gaps on both sides stands alone ("2007 . 1123"), not hugging both.
HUG_MARGIN = 1.1
# A vertical gap above this many median line heights starts a new paragraph.
PARAGRAPH_GAP = 0.9

_OPENING = frozenset("([{")
_CLOSING = frozenset(".,;:!?)]}")
_BRACKETS = frozenset("()[]{}<>")
_PUNCT_HUG = _OPENING | _CLOSING


@dataclass
class Atom:
    """One character cluster (a base character and its combining marks) with its box.

    The box is in PDF points, with the origin at the top left of the page.
    """

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    index: int
    repaired: str | None = None  # the repair rule that changed this character, if any
    font: str = ""  # the font's name, recorded for private-use codes (font_private_use_by_ocr)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def is_space(self) -> bool:
        return self.text.isspace()


@dataclass
class BuiltLine:
    text: str
    bbox: list[float]
    direction: str
    atoms: list[Atom]
    flags: list[str] = field(default_factory=list)
    row: int = 0
    slot: int = 0  # component index within the row, in reading order


@dataclass
class PageLines:
    lines: list[BuiltLine]
    flags: list[str]
    selection_safe: bool
    has_columns: bool
    clear_two_columns: bool


def _bidi_type(ch: str) -> str:
    bidi = unicodedata.bidirectional(ch)
    if bidi in ("R", "AL"):
        return "R"
    if bidi == "L":
        return "L"
    if bidi in ("EN", "AN"):
        return "EN"
    return "N"


def _unit_type(text: str) -> str:
    for ch in text:
        kind = _bidi_type(ch)
        if kind != "N":
            return kind
    return "N"


def is_rtl(text: str) -> bool:
    """RTL when some token has RTL script and RTL tokens are not outnumbered by Latin ones."""
    tokens = text.split()
    rtl = sum(any(_bidi_type(ch) == "R" for ch in token) for token in tokens)
    latin = sum(any("LATIN" in unicodedata.name(ch, "") for ch in token) for token in tokens)
    return rtl > 0 and rtl >= latin


def rows(atoms: list[Atom], median_height: float) -> list[list[Atom]]:
    """Cluster visual baselines, top to bottom, without trusting source line ids."""
    tolerance = max(1.0, median_height * ROW_TOLERANCE)
    ordered = sorted(atoms, key=lambda a: (a.cy, a.x0))
    out: list[list[Atom]] = []
    centers: list[float] = []
    for atom in ordered:
        if not out or abs(atom.cy - centers[-1]) > tolerance:
            out.append([atom])
            centers.append(atom.cy)
            continue
        out[-1].append(atom)
        centers[-1] = statistics.fmean(a.cy for a in out[-1])
    return out


def components(row: list[Atom], median_height: float) -> list[list[Atom]]:
    """Split a visual row only at a clearly column-sized gap. Left to right."""
    left_to_right = sorted(row, key=lambda a: (a.cx, a.index))
    ink = [a for a in left_to_right if not a.is_space]
    if not ink:
        return []
    gap_limit = max(COLUMN_GAP_MIN, median_height * COLUMN_GAP_HEIGHTS)
    groups: list[list[Atom]] = [[]]
    previous: Atom | None = None
    for atom in left_to_right:
        if not atom.is_space and previous is not None and atom.x0 - previous.x1 > gap_limit:
            groups.append([])
        groups[-1].append(atom)
        if not atom.is_space:
            previous = atom
    return [_strip_spaces(g) for g in groups if any(not a.is_space for a in g)]


def _strip_spaces(group: list[Atom]) -> list[Atom]:
    start, end = 0, len(group)
    while start < end and group[start].is_space:
        start += 1
    while end > start and group[end - 1].is_space:
        end -= 1
    return group[start:end]


def _overlap(a: Atom, b: Atom) -> float:
    return min(a.x1, b.x1) - max(a.x0, b.x0)


def visual_words(atoms: list[Atom]) -> list[list[Atom]]:
    """Words of a page, each as its atoms in visual (left to right) order, using the line builder's breaks."""
    ink = [a for a in atoms if not a.is_space]
    if not ink:
        return []
    median_height = statistics.median(a.height for a in ink)
    words: list[list[Atom]] = []
    for row in rows(atoms, median_height):
        for component in components(row, median_height):
            current: list[Atom] = []
            for text, atom in _units(component):
                if text == " ":
                    if current:
                        words.append(current)
                    current = []
                elif atom is not None:
                    current.append(atom)
            if current:
                words.append(current)
    return words


def _side_gap(component: list[Atom], index: int, step: int) -> float | None:
    """The gap between ``component[index]`` and its next ink neighbour in direction ``step`` (+1
    to the right, -1 to the left); None when there is none or a space glyph lies between."""
    atom = component[index]
    j = index + step
    while 0 <= j < len(component):
        other = component[j]
        if other.is_space:
            return None
        return other.x0 - atom.x1 if step > 0 else atom.x0 - other.x1
    return None


def _units(component: list[Atom]) -> list[tuple[str, Atom | None]]:
    """Visual sequence of character units, with word breaks made explicit as spaces.

    Punctuation that may hug a word (``_CLOSING``, ``_OPENING``) is joined to the side it sits
    closer to. In a left-to-right line a comma sits just right of its word; in a right-to-left line,
    just left of it, with the word space on its other side. Joining only by the glyph (no space
    before a comma) dropped that space in RTL text drawn with real glyph widths ("תאגיד,למעט").
    """
    units: list[tuple[str, Atom | None]] = []
    previous: Atom | None = None
    previous_index = -1
    ink = [a for a in component if not a.is_space]
    for index, atom in enumerate(component):
        if atom.is_space:
            # A space glyph drawn over a letter (a geresh placed on it, say) is not a visual gap.
            if any(_overlap(atom, other) > 0.5 * min(atom.x1 - atom.x0, other.x1 - other.x0) for other in ink):
                continue
            if units and units[-1][0] != " ":
                units.append((" ", atom))
            continue
        if previous is not None and units and units[-1][0] != " ":
            gap = atom.x0 - previous.x1
            if gap > WORD_GAP_EM * max(atom.height, previous.height):
                tight = gap <= min(atom.height, previous.height) * TIGHT_JOIN
                # No space between hugging punctuation and its word when tight: the punctuation
                # (right of the gap, or left of it) hugs this side when it sits clearly closer to it
                # than to its other one.
                hugs = False
                if tight and atom.text[:1] in _PUNCT_HUG:
                    other = _side_gap(component, index, +1)
                    hugs = other is None or other > gap * HUG_MARGIN
                if tight and not hugs and previous.text[-1:] in _PUNCT_HUG:
                    other = _side_gap(component, previous_index, -1)
                    hugs = other is None or other > gap * HUG_MARGIN
                if not hugs:
                    units.append((" ", None))
        units.append((atom.text, atom))
        previous = atom
        previous_index = index
    return units


def line_is_rtl(text: str, page_rtl: bool) -> bool:
    """A line's base direction.

    A PDF does not record paragraph direction. A line with Hebrew letters, or with no letters
    at all (an amount, a date), takes the page's direction. A line with Latin letters and no
    Hebrew is LTR. This departs from a per-line majority vote: in a Hebrew document a
    mostly-English line is still an RTL paragraph.
    """
    has_r = any(_bidi_type(ch) == "R" for ch in text)
    has_l = any(_bidi_type(ch) == "L" for ch in text)
    if has_r:
        return page_rtl or not has_l or is_rtl(text)
    if has_l:
        return False
    return page_rtl


def logical_text(component: list[Atom], page_rtl: bool = True) -> tuple[str, str, list[str]]:
    """Inverse bidi for one visual line segment: (text, direction, flags)."""
    units = _units(component)
    texts = [u for u, _ in units]
    types = [_unit_type(u) for u in texts]
    n = len(texts)
    # W4: a single , . : / between digits joins the number. W5: % ₪ next to digits.
    for i in range(n):
        if types[i] == "N" and 0 < i < n - 1 and texts[i] in (",", ".", ":", "/") and types[i - 1] == "EN" and types[i + 1] == "EN":
            types[i] = "EN"
    for i in range(n):
        if types[i] == "N" and all(unicodedata.bidirectional(ch) == "ET" for ch in texts[i]):
            if (i > 0 and types[i - 1] == "EN") or (i + 1 < n and types[i + 1] == "EN"):
                types[i] = "EN"
    rtl = line_is_rtl("".join(texts), page_rtl)
    base = "R" if rtl else "L"
    # Neutrals between two LTR units (Latin or number) join them; otherwise base.
    ltr_like = [("L" if t in ("L", "EN") else t) for t in types]
    resolved = list(ltr_like)
    i = 0
    while i < n:
        if ltr_like[i] != "N":
            i += 1
            continue
        j = i
        while j < n and ltr_like[j] == "N":
            j += 1
        left = ltr_like[i - 1] if i > 0 else base
        right = ltr_like[j] if j < n else base
        side = left if left == right else base
        if rtl and i > 0 and j < n and types[i - 1] == "EN" and types[j] == "EN" and texts[i] == " ":
            # Two numbers apart in an RTL line, the space on the left of what lies between them: the
            # layout of an RTL paragraph ("1, 2" is shown as "2 ,1"; a section number and a note mark,
            # "1.2. 1", as "1 .1.2"). Numbers act as R on neutrals (UAX #9 N1), so they are two runs.
            # Punctuation that touches the left number ("1, 2" drawn left to right) keeps one run.
            side = base
        for k in range(i, j):
            resolved[k] = side
        i = j
    flags: list[str] = []
    order = list(range(n))
    run_type = "L" if rtl else "R"
    if rtl:
        order.reverse()
        resolved_in_order = [resolved[k] for k in order]
    else:
        resolved_in_order = list(resolved)
    out: list[int] = []
    k = 0
    while k < n:
        if resolved_in_order[k] != run_type:
            out.append(order[k])
            k += 1
            continue
        m = k
        while m < n and resolved_in_order[m] == run_type:
            m += 1
        run = order[k:m][::-1]
        if rtl:
            run_types = {types[x] for x in run}
            has_gap = any(texts[x] == " " for x in run)
            if "L" in run_types and "EN" in run_types and has_gap:
                flags.append("bidi_ambiguous_ltr_run")
        out.extend(run)
        k = m
    text = "".join(texts[x] for x in out).strip()
    if rtl and any(ch in _BRACKETS for ch in text):
        # Whether the layer maps mirrored bracket glyphs to the logical character is producer-specific.
        flags.append("rtl_brackets_unverified")
    return text, ("rtl" if rtl else "ltr"), flags


def clear_two_column_gutter(rendered_rows: list[list[BuiltLine]], median_height: float) -> bool:
    """A stable full-page two-column grid (still unverified)."""
    if len(rendered_rows) < 2 or any(len(row) != 2 for row in rendered_rows):
        return False
    tolerance = max(COLUMN_GAP_MIN, median_height * COLUMN_GAP_HEIGHTS)
    for slot in range(2):
        centers = [(row[slot].bbox[0] + row[slot].bbox[2]) / 2 for row in rendered_rows]
        if max(centers) - min(centers) > tolerance:
            return False
    return True


def build_lines(atoms: list[Atom], rtl: bool | None = None) -> PageLines:
    """Lines in reading order, with the page's reading-order flags.

    ``selection_safe`` says when a geometric reading order may be used without review: every atom has
    valid geometry and no unresolved column structure was seen.
    """
    valid = [a for a in atoms if a.x1 >= a.x0 and a.y1 >= a.y0 and a.height > 0]
    invalid = len(atoms) - len(valid)
    ink = [a for a in valid if not a.is_space]
    if not ink:
        return PageLines([], ["reading_order_no_usable_characters"] if atoms else [], False, False, False)
    median_height = statistics.median(a.height for a in ink)
    # ``rtl`` gives the base direction when the caller knows it (a table cell takes the document's).
    page_rtl = is_rtl("".join(a.text for a in ink)) if rtl is None else rtl
    built_rows: list[list[BuiltLine]] = []
    for row_index, row in enumerate(rows(valid, median_height)):
        comps = components(row, median_height)
        # Column direction follows the page script.
        comps.sort(key=lambda c: max(a.x1 for a in c), reverse=page_rtl)
        rendered: list[BuiltLine] = []
        for slot, comp in enumerate(comps):
            text, direction, flags = logical_text(comp, page_rtl)
            ink_atoms = [a for a in comp if not a.is_space]
            bbox = [
                min(a.x0 for a in ink_atoms),
                min(a.y0 for a in ink_atoms),
                max(a.x1 for a in ink_atoms),
                max(a.y1 for a in ink_atoms),
            ]
            rendered.append(BuiltLine(text, bbox, direction, comp, flags, row_index, slot))
        if rendered:
            built_rows.append(rendered)
    has_columns = any(len(row) > 1 for row in built_rows)
    clear_two = has_columns and clear_two_column_gutter(built_rows, median_height)
    if clear_two:
        lines = [row[slot] for slot in range(2) for row in built_rows]
    else:
        lines = [line for row in built_rows for line in row]
    flags: list[str] = []
    if invalid:
        flags.append("reading_order_invalid_geometry")
    if has_columns:
        flags.append("reading_order_column_major_unverified" if clear_two else "reading_order_unresolved_columns")
    return PageLines(lines, flags, invalid == 0 and not has_columns, has_columns, clear_two)


def paragraphs(page: PageLines) -> list[list[BuiltLine]]:
    """Group consecutive lines into paragraphs by vertical gap, column slot and direction."""
    if not page.lines:
        return []
    heights = [line.bbox[3] - line.bbox[1] for line in page.lines]
    median = statistics.median(heights) if heights else 0.0
    groups: list[list[BuiltLine]] = [[page.lines[0]]]
    for line in page.lines[1:]:
        previous = groups[-1][-1]
        gap = line.bbox[1] - previous.bbox[3]
        overlap = min(line.bbox[2], previous.bbox[2]) - max(line.bbox[0], previous.bbox[0])
        same_flow = (
            page.has_columns is False or page.clear_two_columns
        ) and line.slot == previous.slot
        if (
            not same_flow
            or gap < -0.5 * median
            or gap > PARAGRAPH_GAP * median
            or overlap <= 0
            or line.direction != previous.direction
        ):
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups
