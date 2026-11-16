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
    formats=("pdf", "docx"),
    evidence="a private-use code at the start of a line, cell or list label, followed by a space or nothing",
)

RULES: dict[str, Rule] = {rule.rule_id: rule for rule in (SYMBOL_FONT_BULLET,)}


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
