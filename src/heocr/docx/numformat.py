"""Number formats of WordprocessingML lists (``w:numFmt``), including Hebrew counting."""

from __future__ import annotations

_HEBREW_ALPHABET = "אבגדהוזחטיכלמנסעפצקרשת"  # 22 letters, no final forms
_HEBREW_VALUES = (
    (400, "ת"), (300, "ש"), (200, "ר"), (100, "ק"),
    (90, "צ"), (80, "פ"), (70, "ע"), (60, "ס"), (50, "נ"), (40, "מ"), (30, "ל"), (20, "כ"), (10, "י"),
    (9, "ט"), (8, "ח"), (7, "ז"), (6, "ו"), (5, "ה"), (4, "ד"), (3, "ג"), (2, "ב"), (1, "א"),
)
_ROMAN = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CHICAGO = "*†‡§"


def hebrew_numeral(value: int) -> str:
    """``hebrew1``: Hebrew numerals, with 15 and 16 written טו and טז (not יה, יו)."""
    if value <= 0:
        return ""
    out = []
    while value >= 400:
        out.append("ת")
        value -= 400
    hundreds, rest = divmod(value, 100)
    for number, letter in _HEBREW_VALUES:
        if number >= 100 and hundreds * 100 >= number:
            out.append(letter)
            hundreds -= number // 100
    if rest in (15, 16):
        out.append("ט" + ("ו" if rest == 15 else "ז"))
        rest = 0
    for number, letter in _HEBREW_VALUES:
        if number < 100 and rest >= number:
            out.append(letter)
            rest -= number
    return "".join(out)


def hebrew_letter(value: int) -> str:
    """``hebrew2``: the 22 letters in order; past ת the letter repeats (23 = אא), as Word's letter formats do."""
    if value <= 0:
        return ""
    count, index = divmod(value - 1, 22)
    return _HEBREW_ALPHABET[index] * (count + 1)


def roman(value: int) -> str:
    if value <= 0:
        return ""
    out = []
    for number, numeral in _ROMAN:
        while value >= number:
            out.append(numeral)
            value -= number
    return "".join(out)


def latin_letter(value: int) -> str:
    """Word's ``lowerLetter``: a … z, then aa, bb … (repeated, not base 26)."""
    if value <= 0:
        return ""
    count, index = divmod(value - 1, 26)
    return chr(ord("a") + index) * (count + 1)


def ordinal(value: int) -> str:
    suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


# Formats rendered exactly. Anything else falls back to decimal and is flagged.
SUPPORTED = {
    "decimal": str,
    "decimalZero": lambda v: f"{v:02d}" if 0 <= v < 10 else str(v),
    "upperRoman": roman,
    "lowerRoman": lambda v: roman(v).lower(),
    "upperLetter": lambda v: latin_letter(v).upper(),
    "lowerLetter": latin_letter,
    "ordinal": ordinal,
    "hebrew1": hebrew_numeral,
    "hebrew2": hebrew_letter,
    "decimalEnclosedParen": lambda v: f"({v})",
    "decimalEnclosedFullstop": lambda v: f"{v}.",
    "decimalEnclosedCircle": lambda v: _CIRCLED[v - 1] if 1 <= v <= len(_CIRCLED) else str(v),
    "chicago": lambda v: _CHICAGO[(v - 1) % 4] * ((v - 1) // 4 + 1) if v > 0 else "",
    "none": lambda v: "",
}


def format_number(value: int, fmt: str) -> tuple[str, list[str]]:
    """Render ``value`` in ``fmt``; return the text and any flags about the rendering."""
    flags: list[str] = []
    if fmt == "hebrew2" and value > 22:
        flags.append("numbering_hebrew2_beyond_22")
    render = SUPPORTED.get(fmt)
    if render is None:
        flags.append(f"numbering_format_unsupported:{fmt}")
        return str(value), flags
    return render(value), flags
