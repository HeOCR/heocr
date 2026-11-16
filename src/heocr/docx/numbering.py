"""Our own renderer for DOCX automatic numbering (``word/numbering.xml``).

DOCX stores list numbers as rules, not text: read from the XML alone, 0.6% of multi-level
section numbers in the 132-document comparison were right, against 99% when Word rendered
them (heocr-engine proposal ``document-pipeline-package.md`` §1). This module computes the
numbers Word would show.

The counting model:

- **Shared counters:** counters belong to the abstract list (``w:abstractNum``), after
  ``numStyleLink`` is resolved. Every ``w:num`` that points at the same abstract list
  continues the same count.
- **Start overrides:** a ``w:startOverride`` restarts its level once: the first time its
  ``w:num`` numbers a paragraph at that level, the level takes the override value, and
  deeper levels restart as usual. The counters stay shared with every other ``w:num`` of
  the same abstract list, and parent levels keep their values. This agrees with
  LibreOffice's rendering of real tender documents. There, an instance that overrides
  every level but is first used at level 3 keeps its parents (2.1.2.1). Another instance
  restarts level 0, and the other instances of the list continue from it.
- **Level overrides:** a ``w:lvlOverride`` that contains a full ``w:lvl`` replaces the
  level's definition for that ``w:num`` only.
- **Restarts:** numbering a paragraph at level L resets every deeper level D. By default
  this happens when L < D. When D has ``w:lvlRestart`` with value r, it happens when
  L < r, and never when r is 0.
- **Unused levels:** ``%k`` for a level that has not been used yet shows that level's
  start value, and that level then counts as used, so its next item is start + 1. This
  agrees with LibreOffice on real tender documents. The label is flagged
  ``numbering_parent_unused``.
- **Legal numbering:** ``w:isLgl`` renders every number in the level's text as decimal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from defusedxml import ElementTree

from heocr.docx.numformat import format_number

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_LEVELS = 9
_PLACEHOLDER = re.compile(r"%([1-9])")


def _val(element, name: str, default: str | None = None) -> str | None:
    child = element.find(W + name) if element is not None else None
    if child is None:
        return default
    return child.get(W + "val", default)


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _on(element, name: str) -> bool:
    child = element.find(W + name)
    return child is not None and child.get(W + "val", "1") not in ("0", "false", "off")


@dataclass(frozen=True)
class Level:
    ilvl: int
    start: int = 0
    fmt: str = "decimal"
    text: str = ""
    restart: int | None = None  # None: the default rule
    is_lgl: bool = False
    suffix: str = "tab"
    pstyle: str | None = None


@dataclass
class AbstractList:
    abstract_id: str
    levels: dict[int, Level] = field(default_factory=dict)
    style_link: str | None = None
    num_style_link: str | None = None


@dataclass
class ListInstance:
    num_id: str
    abstract_id: str
    start_overrides: dict[int, int] = field(default_factory=dict)
    level_overrides: dict[int, Level] = field(default_factory=dict)


@dataclass(frozen=True)
class Label:
    text: str
    suffix: str  # "tab", "space" or "nothing"
    fmt: str
    flags: tuple[str, ...] = ()


def _level(element) -> Level:
    ilvl = _int(element.get(W + "ilvl"), 0)
    restart_value = _val(element, "lvlRestart")
    return Level(
        ilvl=ilvl,
        # ECMA-376 §17.9.25: an omitted w:start means zero. Word itself always writes it.
        start=_int(_val(element, "start"), 0),
        fmt=_val(element, "numFmt", "decimal") or "decimal",
        text=_val(element, "lvlText", "") or "",
        restart=_int(restart_value, 0) if restart_value is not None else None,
        is_lgl=_on(element, "isLgl"),
        suffix=_val(element, "suff", "tab") or "tab",
        pstyle=_val(element, "pStyle"),
    )


class Numbering:
    """The lists of one document, with the counters that rendering advances."""

    def __init__(self, abstracts: dict[str, AbstractList], instances: dict[str, ListInstance]) -> None:
        self.abstracts = abstracts
        self.instances = instances
        self._counters: dict[str, list[int | None]] = {}
        self._overrides_used: set[tuple[str, int]] = set()

    # -- parsing -------------------------------------------------------------
    @classmethod
    def parse(cls, xml: bytes | None) -> "Numbering":
        if not xml:
            return cls({}, {})
        root = ElementTree.fromstring(xml)
        abstracts: dict[str, AbstractList] = {}
        for element in root.findall(W + "abstractNum"):
            abstract = AbstractList(
                abstract_id=element.get(W + "abstractNumId", ""),
                style_link=_val(element, "styleLink"),
                num_style_link=_val(element, "numStyleLink"),
            )
            for lvl in element.findall(W + "lvl"):
                level = _level(lvl)
                if 0 <= level.ilvl < MAX_LEVELS:
                    abstract.levels[level.ilvl] = level
            abstracts[abstract.abstract_id] = abstract
        instances: dict[str, ListInstance] = {}
        for element in root.findall(W + "num"):
            instance = ListInstance(num_id=element.get(W + "numId", ""), abstract_id=_val(element, "abstractNumId", "") or "")
            for override in element.findall(W + "lvlOverride"):
                ilvl = _int(override.get(W + "ilvl"), 0)
                start = override.find(W + "startOverride")
                if start is not None:
                    instance.start_overrides[ilvl] = _int(start.get(W + "val"), 0)
                lvl = override.find(W + "lvl")
                if lvl is not None:
                    instance.level_overrides[ilvl] = replace(_level(lvl), ilvl=ilvl)
            instances[instance.num_id] = instance
        return cls(abstracts, instances)

    # -- lookup --------------------------------------------------------------
    def _abstract(self, num_id: str) -> AbstractList | None:
        instance = self.instances.get(num_id)
        if instance is None:
            return None
        abstract = self.abstracts.get(instance.abstract_id)
        seen = set()
        while abstract is not None and abstract.num_style_link and abstract.abstract_id not in seen:
            seen.add(abstract.abstract_id)
            linked = next(
                (a for a in self.abstracts.values() if a.style_link == abstract.num_style_link and a is not abstract),
                None,
            )
            if linked is None:
                break
            abstract = linked
        return abstract

    def _levels(self, num_id: str) -> dict[int, Level]:
        abstract = self._abstract(num_id)
        levels = dict(abstract.levels) if abstract else {}
        instance = self.instances.get(num_id)
        if instance:
            levels.update(instance.level_overrides)
        return levels

    def level_for_style(self, num_id: str, style_id: str) -> int | None:
        """The level whose ``w:pStyle`` names ``style_id``, for style-linked lists."""
        for level in self._levels(num_id).values():
            if level.pstyle == style_id:
                return level.ilvl
        return None

    def knows(self, num_id: str) -> bool:
        return self._abstract(num_id) is not None

    # -- rendering -----------------------------------------------------------
    def next_label(self, num_id: str, ilvl: int) -> Label | None:
        """Advance the list's counters for a paragraph at ``ilvl`` and return its label.

        Returns None when ``num_id`` is 0 (numbering removed) or unknown.
        """
        if num_id in ("", "0"):
            return None
        abstract = self._abstract(num_id)
        if abstract is None:
            return None
        ilvl = min(max(ilvl, 0), MAX_LEVELS - 1)
        levels = self._levels(num_id)
        instance = self.instances[num_id]
        counters = self._counters.setdefault(abstract.abstract_id, [None] * MAX_LEVELS)

        def start_of(index: int) -> int:
            if index in instance.start_overrides:
                return instance.start_overrides[index]
            return levels.get(index, Level(ilvl=index)).start

        level = levels.get(ilvl, Level(ilvl=ilvl))
        current = counters[ilvl]
        if ilvl in instance.start_overrides and (num_id, ilvl) not in self._overrides_used:
            self._overrides_used.add((num_id, ilvl))
            counters[ilvl] = instance.start_overrides[ilvl]
        else:
            counters[ilvl] = start_of(ilvl) if current is None else current + 1
        for deeper in range(ilvl + 1, MAX_LEVELS):
            restart = levels.get(deeper, Level(ilvl=deeper)).restart
            limit = deeper if restart is None else restart
            if ilvl < limit:
                counters[deeper] = None

        flags: list[str] = []

        def render(match: re.Match[str]) -> str:
            index = int(match.group(1)) - 1
            ref = levels.get(index, Level(ilvl=index))
            value = counters[index]
            if value is None:
                # Shown at its start value, and from then on counted as used: the next item at
                # that level is start + 1 (checked against LibreOffice on real tender documents).
                value = start_of(index)
                counters[index] = value
                flags.append("numbering_parent_unused")
            fmt = "decimal" if level.is_lgl and ref.fmt not in ("none",) else ref.fmt
            text, more = format_number(value, fmt)
            flags.extend(more)
            return text

        if level.fmt == "bullet":
            text = level.text
            if any(0xE000 <= ord(ch) <= 0xF8FF for ch in text):
                # Symbol-font bullets (U+F0B7 and the like) have no meaning outside the font.
                text = "•"
                flags.append("bullet_symbol_mapped")
        else:
            text = _PLACEHOLDER.sub(render, level.text)
        return Label(text=text, suffix=level.suffix, fmt=level.fmt, flags=tuple(dict.fromkeys(flags)))
