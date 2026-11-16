"""Numbers checked against a second reading (D3d).

A text layer can hold numbers that look right and are wrong: an amount whose digit groups were
reversed (``000,005,8``), a date split into reversed runs. The second, independent reading is
OCR of the line's own image on the rendered page:

- **Which lines:** by default (``check_numbers="suspect"``), only lines holding a
  ``digit_groups_suspect`` token are read a second time. That is the repair's evidence, and it
  costs nothing on documents without one. ``check_numbers="tables"`` adds every table cell
  holding amounts or dates (cells are where amounts live), at a fraction of the cost of
  ``"all"``. With ``check_numbers="all"``, every line holding
  amounts (thousands groups, ₪, %) or dates is read too. That is opt-in: on the dev split it
  more than doubled the time (91 s to 222 s), and Tesseract's own digit errors left 51% of
  numbers unconfirmed. Either way, each line gets one ``--psm 7`` call (the lead's cost rule).
- **Disagreement:** a number the line's OCR reads differently, when that reading is itself
  trustworthy (see ``compare``), is flagged ``number_disagreement``, never resolved by a guess.
  A number OCR could not confirm is counted as unconfirmed, not flagged. Tesseract errs on
  digits too, so even a disagreement only raises a flag.
- **The one repair:** a ``digit_groups_suspect`` token is repaired (``reversed_digit_groups``) only
  when the OCR reads exactly one of its two readings (every character reversed, or the group
  order reversed) and not the token itself. The confirming reading is logged with the repair.
"""

from __future__ import annotations

import re
from collections import Counter

from heocr.textflags import digit_groups_suspect

NUMBER = re.compile(r"(?<![\d.,/:])\d+(?:[.,/:]\d+)*(?![\d.,/:])")
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+|\d\s?(?:%|₪)|₪\s?\d|\d\s?ש[\"״]ח")
_DATE = re.compile(r"(?<!\d)\d{1,2}[./]\d{1,2}[./]\d{2,4}(?!\d)")
_GROUPS = re.compile(r"(?<![\d,])\d+(?:,\d+){2,}(?![\d,])")
PAD_PT = 2.0
LINE_PSM = 7
MIN_DIGITS = 2
MIN_CONFIDENCE = 85.0  # line confidence an OCR reading needs before it can contradict the layer


def suspect(text: str) -> bool:
    """Lines holding a ``digit_groups_suspect`` token: the repair's evidence (the default scope)."""
    return any(digit_groups_suspect(t) for t in _GROUPS.findall(text))


def wanted(text: str, scope: str = "all") -> bool:
    """Lines worth a second reading. ``"suspect"``: only suspect digit groups; ``"all"``: also every
    line with amounts, percentages or dates."""
    if scope == "suspect":
        return suspect(text)
    return bool(_AMOUNT.search(text) or _DATE.search(text) or suspect(text))


def significant(text: str) -> list[str]:
    return [t for t in NUMBER.findall(text) if sum(ch.isdigit() for ch in t) >= MIN_DIGITS]


def readings(token: str) -> tuple[str, str]:
    """The two readings of a reversed digit-group token: every character reversed, or the group order reversed."""
    return token[::-1], ",".join(reversed(token.split(",")))


def _partial(token: str, reading: str) -> bool:
    """A reading of only some of a comma-joined token's parts ("1:05" of "1:05,28.9.2026"):
    OCR missed part of it, which is no evidence that the layer is wrong."""
    parts, got = token.split(","), reading.split(",")
    if len(got) >= len(parts):
        return False
    return any(parts[i : i + len(got)] == got for i in range(len(parts) - len(got) + 1))


def _digits(tokens: list[str]) -> str:
    return "".join(ch for token in tokens for ch in token if ch.isdigit())


def compare(layer: str, ocr: str, confidence: float | None = None) -> tuple[int, int, int, list[tuple[str, str]]]:
    """(numbers checked, agreed, disagreed, digit-group repairs confirmed as (token, reading)).

    A number counts as **disagreed** only when the OCR reading is itself trustworthy: it found
    the same count of numbers, in the same order, at line confidence ``MIN_CONFIDENCE`` or more,
    and the number at that place differs. Otherwise it is unconfirmed, which is counted but not
    flagged: Tesseract misreads small or tight digits often enough (on the dev split it read
    `30.09` as `50.0`, and dropped list digits), so a missing match is no evidence of an error.
    """
    mine = significant(layer)
    theirs = significant(ocr)
    seen = Counter(theirs)
    agreed = disagreed = 0
    repairs: list[tuple[str, str]] = []
    pool = Counter(seen)
    aligned = len(theirs) == len(mine) and confidence is not None and confidence >= MIN_CONFIDENCE
    # The same digits in the same order, split into tokens differently ("1:05,28.9.2026" as one
    # token against two): no evidence of an error, so no disagreement (dev: all 9 with eng).
    same_digits = _digits(mine) == _digits(theirs)
    for position, token in enumerate(mine):
        if pool[token] > 0:
            pool[token] -= 1
            agreed += 1
            continue
        if digit_groups_suspect(token):
            a, b = readings(token)
            confirmed = [r for r in dict.fromkeys((a, b)) if seen[r] > 0]
            if len(confirmed) == 1:
                repairs.append((token, confirmed[0]))
                continue
        if aligned and not same_digits and theirs[position] != token and not _partial(token, theirs[position]):
            disagreed += 1
    return len(mine), agreed, disagreed, repairs
