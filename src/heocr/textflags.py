"""Warning flags on extracted text: reasons not to trust it silently.

Ported from tendex ``text_flags`` (AdanimInstitute/adanim-govil-scraper, ``tendex_l4/extract.py``,
released for porting into heocr under Apache-2.0 by decision 0020, item 4).
"""

from __future__ import annotations

import re
from datetime import datetime

_DATE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
_DIGIT_GROUPS = re.compile(r"(?<![\d,])\d+(?:,\d+)+(?![\d,])")
_TIME = re.compile(r"(?<![\d.:])(\d{1,2}):(\d{2})(?![\d.:])")


def is_private_use(ch: str) -> bool:
    code = ord(ch)
    return 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0x10FFFD


def text_flags(text: str) -> list[str]:
    flags = []
    if text.count("�") > max(2, len(text) * 0.005):
        flags.append("unmapped_characters")
    if any(
        (ord(ch) < 32 and ch not in "\n\r\t\f") or is_private_use(ch) or 0xFDD0 <= ord(ch) <= 0xFDEF for ch in text
    ):
        # Private-use, control and noncharacter codes are not proof of an error, but they are
        # a concrete reason not to trust the font mapping silently.
        flags.append("suspicious_glyph_mapping")
    # Only unambiguous four-digit Gregorian dates are checked, not tender references.
    for day, month, year in _DATE.findall(text):
        try:
            if not 1900 <= int(year) <= 2100:
                raise ValueError("year outside the expected range")
            datetime(int(year), int(month), int(day))
        except ValueError:
            flags.append("implausible_date")
            break
    if any(int(h) > 23 or int(m) > 59 for h, m in _TIME.findall(text)):
        flags.append("implausible_time")
    if any(digit_groups_suspect(token) for token in _DIGIT_GROUPS.findall(text)):
        flags.append("digit_groups_suspect")
    return flags


def digit_groups_suspect(token: str) -> bool:
    """A thousands-grouped number that cannot be one, such as ``000,005,8``.

    Flag only. The token has two readings: reversing every character gives 8,500,000, and
    reversing the group order gives 8,005,000. The volunteer saw both, so no rule picks
    one. D3 repairs it only where an independent reading confirms one of the two.
    """
    groups = token.split(",")
    if groups[0] == "000":
        return True
    # Two-group tokens are left alone ("0,5" is a decimal comma, "2019,2020" a list), and so are
    # lists with no three-digit group ("1,2,3"): those are not thousands groups at all.
    rest = groups[1:]
    return len(groups) >= 3 and any(len(g) == 3 for g in rest) and any(len(g) != 3 for g in rest)
