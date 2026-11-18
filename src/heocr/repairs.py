"""Repairs: a closed list of rules that restore characters the source meant.

A repair is not normalization (heocr-engine decision 0002). Each rule:

- restores a character that the source's font or producer broke;
- fires only on evidence in the content, never on file names;
- is logged in ``block.repairs`` with the code points and counts it changed.

With repairs off (``extract(..., repairs=False)``, ``heocr --no-repairs``), no rule runs, and the
output is the extracted text, byte for byte.

Rules are added here, with a reason, an example and the formats they apply to, and nowhere
else. The plan ``planning/milestones/d2-routes.md`` lists the planned ones.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from heocr.textflags import is_private_use


@dataclass(frozen=True)
class Rule:
    rule_id: str
    reason: str
    example: tuple[str, str]  # (as extracted, as repaired)
    formats: tuple[str, ...]
    evidence: str


SYMBOL_FONT_BULLET = Rule(
    rule_id="symbol_font_bullet",
    reason="Symbol and Wingdings bullets are private-use codes with no meaning outside their font.",
    example=(" סעיף", "• סעיף"),
    formats=("pdf", "docx", "pptx"),
    evidence="a private-use code at the start of a line, cell or list label, followed by a space or nothing",
)

DIRECTION_MARKS = Rule(
    rule_id="direction_marks",
    reason="Word inserts invisible RLM, LRM and ALM marks around numbers and Latin runs; they split tokens such as a section number from its word.",
    example=("בסעיף \u200f2.2", "בסעיף 2.2"),
    formats=("docx", "html", "pptx"),
    evidence="U+200E, U+200F or U+061C in office XML (DOCX, PPTX) or HTML text",
)

LATIN1_ETH_NUN = Rule(
    rule_id="latin1_eth_nun",
    reason="In cp1255 byte 0xF0 is נ; a font whose map was built as Latin-1 yields ð instead.",
    example=("ðספח", "נספח"),
    formats=("pdf",),
    evidence="ð next to a Hebrew letter, in a font that has no נ anywhere in the document (decided per font, not per file)",
)

RTL_BRACKET_ORDER = Rule(
    rule_id="rtl_bracket_order",
    reason="Producers draw a bracket in RTL text with its mirrored glyph, and map the glyph, not the logical character.",
    example=(")באופן השוואתי(", "(באופן השוואתי)"),
    formats=("pdf",),
    evidence=(
        "in the document's RTL text-layer lines, mirroring every bracket makes the bracket order valid in at "
        "least 3 lines and in more than 3 times as many as reading them as they are (decided per document); "
        "a paragraph balanced as read and broken when mirrored is left as read"
    ),
)

LATIN_WRAP_BRACKETS = Rule(
    rule_id="latin_wrap_brackets",
    reason=(
        "A Latin parenthetical inside Hebrew text that wraps onto the next line leaves one bracket on each "
        "line; neither line holds its partner, so either may come out with its mirrored glyph."
    ),
    example=("ההסכם נחתם במסגרת )framework agreements,\nSection 12) ולכן", "ההסכם נחתם במסגרת (framework agreements,\nSection 12) ולכן"),
    formats=("pdf",),
    evidence=(
        "two consecutive lines of one paragraph or table cell, both with Hebrew letters: the first ends in a "
        "stretch with no Hebrew letter holding exactly one round bracket, with no letter or digit between "
        "the last Hebrew letter and it; the next starts with such a stretch holding exactly one, with no "
        "letter or digit between it and the first Hebrew letter; a Latin letter or § lies between the two; "
        "the rest of each line holds an even number of round brackets; and the two do not read as a pair"
    ),
)


FONT_PRIVATE_USE_BY_OCR = Rule(
    rule_id="font_private_use_by_ocr",
    reason="Some fonts map Hebrew letters to private-use codes and name no glyphs; the page itself shows the letter.",
    example=("השרי\uf01a", "השרים"),
    formats=("pdf",),
    evidence=(
        "word-level OCR of the words holding the code agrees letter for letter at every known position, in at "
        "least 3 words with at least 90% agreement; the letter is absent from the font in regular form; "
        "final letters sit at word ends; no two codes share a letter (learned per font, per document; "
        "heocr.pdf.fontmap)"
    ),
)

REVERSED_DIGIT_GROUPS = Rule(
    rule_id="reversed_digit_groups",
    reason="Conversions and some producers reverse an amount's digit groups (000,005,8 for 8,500,000).",
    example=("000,005,8", "8,500,000"),
    formats=("pdf",),
    evidence=(
        "a digit_groups_suspect token whose line, read again by OCR of its own image, gives exactly one of "
        "its two readings (all characters reversed, or the group order reversed) and not the token itself"
    ),
)

CP1255_SHIFTED_FONT = Rule(
    rule_id="cp1255_shifted_font",
    reason=(
        "Some fonts carry Hebrew as its cp1255 byte: read as Latin-1 (שלום as ùìåí), or moved into the "
        "private-use area at U+F000 + the byte, as symbol fonts do. No Hebrew letter is left on the page."
    ),
    example=("ùìåí", "שלום"),
    formats=("pdf",),
    evidence=(
        "decided per font, over the document: the font has no Hebrew letter; at least "
        "SHIFTED_MIN_WORDS words consist only of codes in U+00E0-U+00FA (or U+F0E0-U+F0FA), and they are "
        "at least SHIFTED_WORD_SHARE of the font's words; and the codes of the five final letters (ך ם ן ף ץ) "
        "stand at a word's edge at least SHIFTED_FINAL_EDGE of the time, consistently at one edge"
    ),
)


def cp1255_shifted_font(text: str) -> tuple[str, Counter]:
    """Every Latin-1 or private-use cp1255 code → its Hebrew letter. The per-font evidence is checked
    by the reader (``shifted_fonts_of``); this applies the change to text it chose."""
    changes: Counter = Counter()
    out = []
    for ch in text:
        letter = cp1255_letter(ord(ch))
        if letter is not None:
            changes[(f"U+{ord(ch):04X}", letter)] += 1
            out.append(letter)
        else:
            out.append(ch)
    return "".join(out), changes


def cp1255_letter(code: int) -> str | None:
    """The Hebrew letter whose cp1255 byte is ``code`` (or ``code`` - 0xF000), else None."""
    byte = code - 0xF000 if code >= 0xF000 else code
    if 0xE0 <= byte <= 0xFA:
        return bytes([byte]).decode("cp1255")
    return None


RULES: dict[str, Rule] = {
    rule.rule_id: rule
    for rule in (SYMBOL_FONT_BULLET, DIRECTION_MARKS, LATIN1_ETH_NUN, RTL_BRACKET_ORDER, LATIN_WRAP_BRACKETS, FONT_PRIVATE_USE_BY_OCR, REVERSED_DIGIT_GROUPS, CP1255_SHIFTED_FONT)
}


def record(rule: Rule, changes: Counter) -> list[dict]:
    """Log entries for one rule: one per changed code point, with its count."""
    return [
        {"rule": rule.rule_id, "from": source, "to": target, "count": count}
        for (source, target), count in sorted(changes.items())
    ]


def symbol_font_bullet(text: str) -> tuple[str, Counter]:
    """Map a leading symbol-font bullet to •. Returns the text and the changes made."""
    changes: Counter = Counter()
    if text and is_private_use(text[0]) and (len(text) == 1 or text[1].isspace()):
        changes[(f"U+{ord(text[0]):04X}", "•")] += 1
        text = "•" + text[1:]
    return text, changes


_DIRECTION_MARKS = {"\u200e": "U+200E", "\u200f": "U+200F", "\u061c": "U+061C"}


def direction_marks(text: str) -> tuple[str, Counter]:
    """Remove RLM, LRM and ALM. Returns the text and the changes made."""
    changes: Counter = Counter()
    for mark, code in _DIRECTION_MARKS.items():
        count = text.count(mark)
        if count:
            changes[(code, "")] += count
            text = text.replace(mark, "")
    return text, changes


def latin1_eth_nun(text: str) -> tuple[str, Counter]:
    """ð → נ. The per-font evidence is checked by the reader; this applies the change to text it chose."""
    changes: Counter = Counter()
    count = text.count("ð")
    if count:
        changes[("U+00F0", "נ")] += count
        text = text.replace("ð", "נ")
    return text, changes


MIRROR = _MIRROR = str.maketrans("()[]{}", ")(][}{")
_PAIRS = {")": "(", "]": "[", "}": "{"}


def bracket_order_valid(text: str) -> bool | None:
    """Whether the brackets in a text are balanced and in order; None when it has none."""
    if not any(ch in "()[]{}" for ch in text):
        return None
    stack = []
    for ch in text:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != _PAIRS[ch]:
                return False
    return not stack


_LABEL_PAIR = re.compile(r"\([\u05d0-\u05ea0-9]{1,3}\)")


def bracket_labels(text: str) -> int:
    """Section labels in brackets, "(א)" "(1)" "(ב1)": short pairs that Hebrew legal text is full of.
    Counted as read and mirrored, they tell a paragraph's bracket order better than balance does: a
    parenthesis closed from the paragraph before and one opened into the next pair up falsely."""
    return len(_LABEL_PAIR.findall(text))


# Brackets of Latin text inside an RTL line ("Article 76(1)", "(Annual Report)", "§75(1)"): line building
# reads them as shown (lines._latin_brackets), whatever the producer mapped; a pair it left balanced is
# the Latin text's own.
_LATIN_STRETCH = re.compile(r"[^\u05d0-\u05ea]+")
_LATIN_END = re.compile(r"[^\u05d0-\u05ea]*$")


def latin_sub_levels(text: str, lead: bool = False) -> set[int]:
    """Positions of the brackets that are a Latin text's own in a logical RTL line: pairs balanced as
    read within a stretch with no Hebrew letter that holds a Latin letter or §; with ``lead``, every
    bracket of the stretch that starts the line, the end of a reference continued from the line before.
    They are never mirrored."""
    keep: set[int] = set()
    for stretch in _LATIN_STRETCH.finditer(text):
        body, at = stretch.group(), stretch.start()
        if lead and at == 0:
            keep.update(i for i, ch in enumerate(body) if ch in "()")
            continue
        if not re.search(r"[A-Za-z§]", body):
            continue
        opened: list[int] = []
        for i, ch in enumerate(body):
            if ch == "(":
                opened.append(i)
            elif ch == ")" and opened:
                keep.update((at + opened.pop(), at + i))
    return keep


def mirror_brackets(text: str, lead: bool = False) -> str:
    """Every bracket mirrored, except a Latin reference's sub-levels (``latin_sub_levels``)."""
    keep = latin_sub_levels(text, lead)
    return "".join(ch if i in keep else ch.translate(_MIRROR) for i, ch in enumerate(text))


def rtl_bracket_order(text: str, lead: bool = False) -> tuple[str, Counter]:
    """Mirror every bracket but a Latin reference's sub-levels. The per-document evidence is checked
    by the reader. ``lead``: the line continues a Latin reference from the line before it."""
    changes: Counter = Counter()
    keep = latin_sub_levels(text, lead)
    for i, ch in enumerate(text):
        if ch in "()[]{}" and i not in keep:
            changes[(f"U+{ord(ch):04X}", ch.translate(_MIRROR))] += 1
    return mirror_brackets(text, lead), changes


def continues_latin(previous: str) -> bool:
    """Whether a line ends inside a Latin reference the next line continues: its last stretch with no
    Hebrew letter holds a Latin letter or § and an odd number of round brackets ("(Art." wrapped)."""
    tail = _LATIN_END.search(previous).group()
    return bool(re.search(r"[A-Za-z§]", tail)) and (tail.count("(") + tail.count(")")) % 2 == 1


_ROUND = re.compile(r"[()]")
_HEBREW_LETTER = re.compile(r"[\u05d0-\u05ea]")
_HEAD = re.compile(r"^[^\u05d0-\u05ea]*")


def latin_wrap_brackets(text: str) -> tuple[str, Counter]:
    """Pair the two brackets of a Latin parenthetical split by a line wrap ("(framework agreements," /
    "Section 12)") in a paragraph's or cell's lines, joined by newlines: the first opens, the second
    closes. The conditions are ``LATIN_WRAP_BRACKETS.evidence``; whether the document mirrored its
    brackets does not matter, since a lone bracket has no partner to check it against."""
    lines = text.split("\n")
    changes: Counter = Counter()
    for k in range(len(lines) - 1):
        first, second = lines[k], lines[k + 1]
        if not (_HEBREW_LETTER.search(first) and _HEBREW_LETTER.search(second)):
            continue
        tail_at, head_end = _LATIN_END.search(first).start(), _HEAD.match(second).end()
        tail, head = first[tail_at:], second[:head_end]
        opens = [m.start() for m in _ROUND.finditer(tail)]
        closes = [m.start() for m in _ROUND.finditer(head)]
        if len(opens) != 1 or len(closes) != 1:
            continue
        o, c = opens[0], closes[0]
        if re.search(r"\w", tail[:o]) or re.search(r"\w", head[c + 1 :]):
            continue  # a bracket hugs the Latin run on its Hebrew side, or it is not this reference's
        if len(_ROUND.findall(first[:tail_at])) % 2 or len(_ROUND.findall(second[head_end:])) % 2:
            continue  # it may pair with a bracket elsewhere on its line
        if not re.search(r"[A-Za-z§]", tail[o + 1 :] + head[:c]):
            continue
        if tail[o] == "(" and head[c] == ")":
            continue
        for ch, want in ((tail[o], "("), (head[c], ")")):
            if ch != want:
                changes[(f"U+{ord(ch):04X}", want)] += 1
        lines[k] = first[: tail_at + o] + "(" + first[tail_at + o + 1 :]
        lines[k + 1] = second[:c] + ")" + second[c + 1 :]
    return "\n".join(lines), changes


def brackets_mirrored_in(units: list[str]) -> bool:
    """The rtl_bracket_order evidence over a document's RTL lines."""
    as_read = mirrored = 0
    for text in units:
        if bracket_order_valid(text):
            as_read += 1
        if bracket_order_valid(mirror_brackets(text)):
            mirrored += 1
    return mirrored >= 3 and mirrored > 3 * as_read


def font_private_use_by_ocr(text: str, mapping: dict[str, str] | None = None) -> tuple[str, Counter]:
    """Apply a learned code -> letter map (the learning is ``heocr.pdf.fontmap``)."""
    mapping = mapping if mapping is not None else {"\uf01a": "ם"}
    changes: Counter = Counter()
    for code, letter in mapping.items():
        count = text.count(code)
        if count:
            changes[(f"U+{ord(code):04X}", letter)] += count
            text = text.replace(code, letter)
    return text, changes


def reversed_digit_groups(text: str) -> tuple[str, Counter]:
    """The repair's text change; the confirming OCR reading is checked in ``heocr.pdf.numbers``."""
    changes: Counter = Counter()
    if text.startswith("000,"):
        fixed = text[::-1]
        changes[(text, fixed)] += 1
        return fixed, changes
    return text, changes
