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

import re
import statistics
import unicodedata
from collections import defaultdict
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
# A word break wider than this share of the loose box height, still under a column gap, between two words that
# hold digits, is a cell gap: table cells set side by side ("QX-418" and "62" in an RTL table). Bidi treats it
# as a segment separator (a tab, UAX #9 L1), so no direction run crosses it and each cell keeps its place in the row.
CELL_GAP_HEIGHTS = 0.8

_OPENING = frozenset("([{")
_CLOSING = frozenset(".,;:!?)]}")
_BRACKETS = frozenset("()[]{}<>")
_PUNCT_HUG = _OPENING | _CLOSING
_HEBREW_LETTER = re.compile(r"[א-ת]")


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
    reflected_number: tuple[int, int] | None = None  # first stream index and length of this numeric object

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


def _latin_word(text: str) -> bool:
    """A word of Latin text: Latin letters and no digit. A code that mixes them ("TR-905") is not one."""
    return any(_bidi_type(ch) == "L" for ch in text) and not any(ch.isdigit() for ch in text)


def _follows_document(atoms: list[Atom]) -> bool:
    """Whether a band with no Hebrew letters takes the document's direction: amounts, dates and codes
    ("TR-905" among a column of amounts), not an English passage. Fewer than half its words are Latin words."""
    if any(_bidi_type(ch) == "R" for a in atoms for ch in a.text):
        return False
    words = [w for w in visual_words(atoms) if w]
    latin = sum(_latin_word("".join(a.text for a in w)) for w in words)
    return 2 * latin < len(words)


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


def _word_gap(ink: list[Atom]) -> float:
    """Infer word gaps on Hebrew lines with uniform tracking and repeated wider spaces.

    Some producers draw a justified line as individually positioned letters. Added tracking can make
    every letter gap look like a word break. Condensed tracking can make even the word boxes overlap.
    Use the two bands of gaps only when both are visible in the geometry of the line.
    """
    if len(ink) < 10 or any(_unit_type(atom.text) in ("L", "EN") for atom in ink):
        return WORD_GAP_EM
    gaps = [
        (right.x0 - left.x1) / max(left.height, right.height)
        for left, right in zip(ink, ink[1:])
        if _HEBREW_LETTER.match(left.text) and _HEBREW_LETTER.match(right.text)
        and max(left.height, right.height) > 0
    ]
    if len(gaps) < 9:
        return WORD_GAP_EM
    tracking = statistics.median(gaps)
    tolerance = 0.02
    if abs(tracking) < 0.07 or sum(abs(gap - tracking) <= tolerance for gap in gaps) < 0.75 * len(gaps):
        return WORD_GAP_EM
    wider = [gap for gap in gaps if gap - tracking > 2 * tolerance]
    # Three short words can supply two repeated word gaps.
    if len(wider) < 2:
        return WORD_GAP_EM
    typical_space = statistics.median(wider)
    if sum(abs(gap - typical_space) <= tolerance for gap in wider) < 0.75 * len(wider):
        return WORD_GAP_EM
    return (tracking + typical_space) / 2


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
    word_gap = _word_gap(ink)
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
            if gap > word_gap * max(atom.height, previous.height):
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


def _latin_around(types: list[str], texts: list[str], i: int, j: int, lead: bool = False) -> bool:
    """Whether the non-Hebrew stretch around units ``i``..``j`` (up to the nearest R unit on each
    side) holds a Latin letter or a section sign. With ``lead``, a stretch that reaches the line's
    right end (an RTL line's logical start) continues the previous line's Latin text."""
    k = i - 1
    while k >= 0 and types[k] not in ("R", "S"):
        if types[k] == "L" or "§" in texts[k]:
            return True
        k -= 1
    k = j
    while k < len(types) and types[k] not in ("R", "S"):
        if types[k] == "L" or "§" in texts[k]:
            return True
        k += 1
    if lead and k == len(types):
        return True
    return any("§" in texts[k] for k in range(i, j))


SUB_LEVEL_UNITS = 3  # a sub-level in brackets after a number ("76(1)", "77(2)(a)", "(iv)") holds at most this many glyphs
_SUB_LEVEL_PAIRS = {"(": ("(", ")"), ")": ("(", ")"), "[": ("[", "]"), "]": ("[", "]")}


def _latin_brackets(texts: list[str], types: list[str], resolved: list[str], lead: bool) -> None:
    """Brackets of a Latin reference inside an RTL line ("Article 76(1)", "§75(1)", "(Annual Report)").

    The page shows them as written, but producers map their glyphs either way, sometimes both ways
    in one document: a sub-level after a number may come as ")1(" and a Latin parenthetical as
    "(Annual Report(". Within a stretch of non-Hebrew units that holds a Latin letter or § (or, with
    ``lead``, continues the previous line's):

    - a sub-level attached to a number or letter (``SUB_LEVEL_UNITS`` glyphs at most, between round or
      square brackets) is part of the Latin run, read left to right as it is shown;
    - the brackets hugging the run on both sides, when the layer gives the same character for
      both, are a pair: the right one opens it, as an RTL paragraph shows it.
    """
    n = len(texts)
    i = 0
    while i < n:
        if types[i] in ("R", "S"):
            i += 1
            continue
        j = i
        while j < n and types[j] not in ("R", "S"):
            j += 1
        if any(types[k] == "L" or "§" in texts[k] for k in range(i, j)) or (lead and j == n):
            attached: set[int] = set()
            k = i + 1
            while k < j:
                pair = _SUB_LEVEL_PAIRS.get(texts[k])
                if pair and (types[k - 1] in ("L", "EN") or (k - 1) in attached):
                    m = k + 1
                    while m < j and m - k - 1 < SUB_LEVEL_UNITS and types[m] in ("L", "EN"):
                        m += 1
                    if k + 1 < m < j and texts[m] in pair:
                        texts[k], texts[m] = pair
                        for x in range(k, m + 1):
                            resolved[x] = "L"
                        attached.update((k, m))
                        k = m + 1
                        continue
                k += 1
            core = [k for k in range(i, j) if types[k] in ("L", "EN") or k in attached]
            if core:
                p, q = core[0] - 1, core[-1] + 1
                if p >= i and q < j and texts[p] in "()" and texts[p] and texts[p] == texts[q]:
                    texts[p], texts[q] = ")", "("
                else:
                    _number_beside_latin_pair(texts, types, resolved, i, j, attached)
        i = j


def _number_beside_latin_pair(texts: list[str], types: list[str], resolved: list[str], i: int, j: int, attached: set[int]) -> None:
    """A number set beside a Latin parenthetical, outside its brackets: "משנת 2018 (Assigning
    responsibilities), ..." in an RTL line. The page shows the number, then the parenthetical, right
    to left. The brackets take the paragraph's direction (UAX #9 N0: the number before them counts as
    R), so the number is a run of its own; read as one left-to-right run with the Latin text, it came
    out after it, with a bracket each side ("Assigning responsibilities) 2018)"). The pair is the
    brackets hugging the Latin letters only, when the rest of the stretch outside them is numbers and
    neutrals. Whatever characters the layer gives them (the same one twice, as LibreOffice writes
    them, or each glyph's own), the right one opens the pair, as an RTL paragraph shows it."""
    latin = [k for k in range(i, j) if types[k] == "L" or k in attached]
    if not latin:
        return
    p, q = latin[0] - 1, latin[-1] + 1
    if not (p >= i and q < j and texts[p] in ("(", ")") and texts[q] in ("(", ")")):
        return
    if any(texts[k] in _BRACKETS for k in range(p + 1, q) if k not in attached):
        return  # another bracket inside: not one parenthetical
    outside = [k for k in range(i, j) if not p <= k <= q]
    if not any(types[k] == "EN" for k in outside) or any(types[k] == "L" for k in outside):
        return
    texts[p], texts[q] = ")", "("
    resolved[p] = resolved[q] = "R"
    # The neutrals between a bracket and the number take the paragraph's direction too.
    for k in outside:
        if types[k] == "N":
            resolved[k] = "R"


def _cell_gaps(units: list[tuple[str, Atom | None]]) -> list[str]:
    """The units' texts, with each cell gap made a tab.

    A cell gap is a word break wider than ``CELL_GAP_HEIGHTS`` between two words of amounts, dates or codes
    ("QX-418", "62"): each holds a digit and no Hebrew letter. Not inside Latin text: a stretch of words
    without Hebrew that holds a Latin word ("Ps 29, 1") keeps one run, whatever its spacing (an OCR layer's
    word boxes can be set far apart)."""
    texts = [u for u, _ in units]
    words: list[tuple[int, int, str]] = []  # [start, end) of each word's units, and its text
    k = 0
    while k < len(units):
        if units[k][0] == " ":
            k += 1
            continue
        j = k
        while j < len(units) and units[j][0] != " ":
            j += 1
        words.append((k, j, "".join(texts[k:j])))
        k = j
    # Letters only: a direction mark (RLM) is not a Hebrew word.
    hebrew = [any(_bidi_type(ch) == "R" and unicodedata.category(ch).startswith("L") for ch in w) for _, _, w in words]
    # Words in a stretch (between Hebrew words) that holds a Latin word.
    in_latin = [False] * len(words)
    i = 0
    while i < len(words):
        if hebrew[i]:
            i += 1
            continue
        j = i
        while j < len(words) and not hebrew[j]:
            j += 1
        if any(_latin_word(words[x][2]) for x in range(i, j)):
            for x in range(i, j):
                in_latin[x] = True
        i = j
    for x in range(len(words) - 1):
        (a0, a1, left_text), (b0, b1, right_text) = words[x], words[x + 1]
        if hebrew[x] or hebrew[x + 1] or in_latin[x]:
            continue
        if not (any(ch.isdigit() for ch in left_text) and any(ch.isdigit() for ch in right_text)):
            continue
        left = [atom for _, atom in units[a0:a1] if atom is not None]
        right = [atom for _, atom in units[b0:b1] if atom is not None]
        if not left or not right:
            continue
        last, first = max(left, key=lambda a: a.x1), min(right, key=lambda a: a.x0)
        if first.x0 - last.x1 > CELL_GAP_HEIGHTS * max(first.height, last.height):
            texts[a1] = "\t"
    return texts


def _cells_follow_page(texts: list[str], types: list[str]) -> bool:
    """Whether a piece of a row split by cell gaps, with no Hebrew, is laid out in the page's direction.

    A table row of codes and amounts ("QX-418", "62", "14.03.2022") in an RTL table has a few Latin
    letters and no Hebrew, which alone would make it an LTR line. Its cells are set in the table's
    direction. It stays LTR when half its cells or more are Latin words (an English table's row).
    """
    if "\t" not in texts or "R" in types:
        return False
    cells = "".join(texts).split("\t")
    latin = sum(any(_latin_word(word) for word in cell.split()) for cell in cells)
    return 2 * latin < len(cells)


def _unreflect_numbers(units: list[tuple[str, Atom | None]], texts: list[str]) -> None:
    # Restore only complete tokens. Keep the actual boxes for geometry and coverage.
    positions: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (_, atom) in enumerate(units):
        if atom is not None and atom.reflected_number is not None:
            positions[atom.reflected_number].append(index)
    for (_, length), slots in positions.items():
        first, last = slots[0], slots[-1]
        if len(slots) == length == last - first + 1:
            texts[first:last + 1] = reversed(texts[first:last + 1])


def logical_text(component: list[Atom], page_rtl: bool = True, latin_lead: bool = False) -> tuple[str, str, list[str]]:
    """Inverse bidi for one visual line segment: (text, direction, flags). ``latin_lead``: the line
    continues a Latin reference from the line before it in its paragraph or cell."""
    units = _units(component)
    texts = _cell_gaps(units)
    # Tesseract's invisible reflected number objects have logical text but RTL coordinates.
    # Present those tokens in the usual LTR visual order before applying inverse bidi.
    if any(atom.reflected_number is not None for atom in component):
        _unreflect_numbers(units, texts)
    # A cell gap is a segment separator: it and its neighbours take the line's direction (UAX #9 L1).
    types = ["S" if u == "\t" else _unit_type(u) for u in texts]
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
    if not rtl and page_rtl and _cells_follow_page(texts, types):
        rtl = True
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
        # A line that continues a Latin reference starts (at its right end) inside that Latin run.
        right = ltr_like[j] if j < n else ("L" if rtl and latin_lead else base)
        side = left if left == right else base
        if rtl and i > 0 and j < n and types[i - 1] == "EN" and types[j] == "EN" and texts[i] == " ":
            # Two numbers apart in an RTL line, the space on the left of what lies between them: the
            # layout of an RTL paragraph ("1, 2" is shown as "2 ,1"; a section number and a note mark,
            # "1.2. 1", as "1 .1.2"). Numbers act as R on neutrals (UAX #9 N1), so they are two runs.
            # Punctuation that touches the left number ("1, 2" drawn left to right) keeps one run.
            side = base
        elif (rtl and i > 0 and j < n and types[i - 1] == "EN" and types[j] == "EN" and any(texts[k] in _BRACKETS for k in range(i, j))
              and not _latin_around(types, texts, i, j, latin_lead)):
            # A bracket between two numbers in an RTL line: "11(5)" is shown as "(5)11". A bracket pair
            # takes the paragraph's direction when it holds a number (UAX #9 N0, numbers counting as R),
            # so the brackets are not part of either number's run. Not inside a Latin run ("Article
            # 76(1)", "§75(1)"): there the numbers belong to the Latin text's own left-to-right order.
            side = base
        for k in range(i, j):
            resolved[k] = side
        i = j
    if rtl:
        _latin_brackets(texts, types, resolved, latin_lead)
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
    text = "".join(texts[x] for x in out).replace("\t", " ").strip()
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


def build_lines(atoms: list[Atom], rtl: bool | None = None, default_rtl: bool | None = None) -> PageLines:
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
    # ``default_rtl`` is the document's direction, for a band with no Hebrew letter whose words are mostly
    # amounts, dates and codes: it is laid out in the document's direction (an RTL table's all-number
    # rows came out with their cells in reverse order, and one Latin code among them flipped the band).
    if rtl is not None:
        page_rtl = rtl
    elif default_rtl is not None and _follows_document(ink):
        page_rtl = default_rtl
    else:
        page_rtl = is_rtl("".join(a.text for a in ink))
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


def carry_latin_context(lines: list[BuiltLine]) -> None:
    """Re-read an RTL line that continues a Latin reference from the line before it (a paragraph's or
    a table cell's): "(Art. 76(2))" wrapped in a narrow cell leaves "76(2))" alone on a line with no
    Latin letter, where the bracket rules for Hebrew text would apply."""
    from heocr.repairs import continues_latin

    for previous, line in zip(lines, lines[1:]):
        if line.direction == "rtl" and continues_latin(previous.text):
            text, _, flags = logical_text(line.atoms, True, latin_lead=True)
            if text != line.text:
                line.text, line.flags = text, flags


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
    for group in groups:
        carry_latin_context(group)
    return groups
