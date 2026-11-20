"""The parts of ``word/styles.xml`` that decide headings, numbering and visibility."""

from __future__ import annotations

import re
from dataclasses import dataclass

from defusedxml import ElementTree

from heocr.docx.numbering import W

_HEADING_NAME = re.compile(r"\Aheading\s*([1-9])\Z", re.I)
_MAX_DEPTH = 32


def toggle(rpr, name: str) -> bool | None:
    """The value of an on/off run property, or None when it is not set."""
    if rpr is None:
        return None
    element = rpr.find(W + name)
    if element is None:
        return None
    return element.get(W + "val", "1") not in ("0", "false", "off")


@dataclass
class Style:
    style_id: str
    kind: str
    name: str
    based_on: str | None
    ppr: object | None
    rpr: object | None


class Styles:
    def __init__(self, styles: dict[str, Style], default_paragraph: str | None, default_rpr) -> None:
        self.styles = styles
        self.default_paragraph = default_paragraph
        self.default_rpr = default_rpr

    @classmethod
    def parse(cls, xml: bytes | None) -> "Styles":
        if not xml:
            return cls({}, None, None)
        root = ElementTree.fromstring(xml)
        styles: dict[str, Style] = {}
        default_paragraph = None
        for element in root.findall(W + "style"):
            style_id = element.get(W + "styleId", "")
            kind = element.get(W + "type", "paragraph")
            name_el = element.find(W + "name")
            based_el = element.find(W + "basedOn")
            styles[style_id] = Style(
                style_id=style_id,
                kind=kind,
                name=name_el.get(W + "val", "") if name_el is not None else "",
                based_on=based_el.get(W + "val") if based_el is not None else None,
                ppr=element.find(W + "pPr"),
                rpr=element.find(W + "rPr"),
            )
            if kind == "paragraph" and element.get(W + "default") in ("1", "true"):
                default_paragraph = style_id
        defaults = root.find(W + "docDefaults")
        default_rpr = None
        if defaults is not None:
            holder = defaults.find(W + "rPrDefault")
            default_rpr = holder.find(W + "rPr") if holder is not None else None
        return cls(styles, default_paragraph, default_rpr)

    def chain(self, style_id: str | None, *, paragraph: bool = True) -> list[Style]:
        """The style and its ``basedOn`` ancestors, nearest first."""
        if style_id is None and paragraph:
            style_id = self.default_paragraph
        out: list[Style] = []
        seen: set[str] = set()
        while style_id and style_id in self.styles and style_id not in seen and len(out) < _MAX_DEPTH:
            seen.add(style_id)
            style = self.styles[style_id]
            out.append(style)
            style_id = style.based_on
        return out

    # -- paragraph properties through the style chain ------------------------
    def outline_level(self, style_id: str | None) -> int | None:
        for style in self.chain(style_id):
            if style.ppr is not None:
                element = style.ppr.find(W + "outlineLvl")
                if element is not None:
                    try:
                        return int(element.get(W + "val", "9"))
                    except ValueError:
                        return None
            match = _HEADING_NAME.match(style.name)
            if match:
                return int(match.group(1)) - 1
        return None

    def num_pr(self, style_id: str | None) -> tuple[str | None, int | None, list[str]]:
        """(numId, ilvl, the style chain's ids) from the style chain.

        ECMA-376 inherits ``numId`` and ``ilvl`` independently: each comes from the nearest
        style in the ``basedOn`` chain that sets it. A style may set only ``ilvl`` and take
        its list from its parent.
        """
        num_id: str | None = None
        ilvl: int | None = None
        chain = self.chain(style_id)
        for style in chain:
            element = style.ppr.find(W + "numPr") if style.ppr is not None else None
            if element is None:
                continue
            num_el, ilvl_el = element.find(W + "numId"), element.find(W + "ilvl")
            if num_id is None and num_el is not None:
                num_id = num_el.get(W + "val")
            if ilvl is None and ilvl_el is not None:
                value = ilvl_el.get(W + "val", "")
                ilvl = int(value) if value.isdigit() else None
            if num_id is not None and ilvl is not None:
                break
        return num_id, ilvl, [style.style_id for style in chain]

    def bidi(self, style_id: str | None) -> bool | None:
        for style in self.chain(style_id):
            value = toggle(style.ppr, "bidi")
            if value is not None:
                return value
        return None

    def is_toc(self, style_id: str | None) -> bool:
        for style in self.chain(style_id)[:1]:
            name = style.name.strip().lower()
            if name.startswith("toc") or name.startswith("תוכן עניינים"):
                return True
        return bool(style_id and style_id.lower().startswith("toc"))

    # -- run properties ------------------------------------------------------
    def run_property(self, direct_rpr, rstyle: str | None, pstyle: str | None, name: str):
        """The element for a run property, looked up direct, then character style, then paragraph style, then defaults."""
        sources = [direct_rpr]
        sources += [s.rpr for s in self.chain(rstyle, paragraph=False)]
        sources += [s.rpr for s in self.chain(pstyle)]
        sources.append(self.default_rpr)
        for rpr in sources:
            if rpr is None:
                continue
            element = rpr.find(W + name)
            if element is not None:
                return element
        return None

    def hidden(self, direct_rpr, rstyle: str | None, pstyle: str | None) -> bool:
        for name in ("vanish", "specVanish"):
            element = self.run_property(direct_rpr, rstyle, pstyle, name)
            if element is not None and element.get(W + "val", "1") not in ("0", "false", "off"):
                return True
        return False

    def white(self, direct_rpr, rstyle: str | None, pstyle: str | None) -> bool:
        element = self.run_property(direct_rpr, rstyle, pstyle, "color")
        if element is None:
            return False
        return (element.get(W + "val") or "").upper() == "FFFFFF" or element.get(W + "themeColor") in ("background1", "light1")
