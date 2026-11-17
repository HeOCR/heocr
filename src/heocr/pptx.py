"""The PPTX route (D4e): our own reading of PresentationML, slide by slide.

- **Slides in order** (the presentation's slide list). Every block carries its slide's number
  (``slide``, from 1). A hidden slide is read and flagged ``hidden_slide``.
- **Reading order by position:** a slide's shapes are grouped into rows (shapes whose vertical
  extents overlap by half the smaller one), rows top to bottom, and within a row right to left
  in an RTL deck (left to right otherwise). The title comes first. Groups are ordered the same way
  inside, with their child coordinates mapped to the slide. A placeholder without its own
  position takes its layout's, then its master's.
- **Text:** a block per paragraph, with line breaks and fields (slide number, date) kept. The
  title is a heading (level 1); explicit bullets (``buChar``) and numbering (``buAutoNum``) are
  labels. A paragraph's ``rtl`` decides its direction, else its first strong character. Footer,
  date and slide-number placeholders on a slide get the role ``footer``.
- **Tables** are table blocks, with ``gridSpan``/``rowSpan`` as spans and ``merged_above`` cells
  in the positions a merge covers in later rows, as in the other routes.
- **Speaker notes** get the role ``notes`` (the notes slide's body placeholder only).
- **Pictures** are read by OCR, as DOCX images are (D2e2a). SmartArt text is read from its data
  part (flagged ``from_smartart``). Charts, media and OLE objects go to ``unread``.
- **Hidden shapes** are read and flagged ``hidden_shape``, never dropped silently.
- **Limits:** at most ``max_pages`` slides (``contract.MAX_PAGES``); the rest are one ``unread``
  entry (``page_limit``).
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field

import defusedxml
from defusedxml import ElementTree

from heocr import repairs
from heocr.contract import Block, Cell, Document, Unread, page_limit
from heocr.docx.numformat import hebrew_letter, hebrew_numeral, latin_letter, roman
from heocr.docx.reader import DocxError, _Package, image_blocks

TOOL = "heocr.pptx"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DGM = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
_STRICT = (
    (b"http://purl.oclc.org/ooxml/presentationml/main", b"http://schemas.openxmlformats.org/presentationml/2006/main"),
    (b"http://purl.oclc.org/ooxml/drawingml/main", b"http://schemas.openxmlformats.org/drawingml/2006/main"),
    (b"http://purl.oclc.org/ooxml/officeDocument/relationships", b"http://schemas.openxmlformats.org/officeDocument/2006/relationships"),
    (b"http://purl.oclc.org/ooxml/drawingml/diagram", b"http://schemas.openxmlformats.org/drawingml/2006/diagram"),
)
_URI_TABLE = "/drawingml/2006/table"
_URI_CHART = "/drawingml/2006/chart"
_URI_DIAGRAM = "/drawingml/2006/diagram"
ROW_OVERLAP = 0.5  # shapes are on one row when their vertical extents overlap by this share of the smaller one
TITLE_TYPES = ("title", "ctrTitle")
FOOTER_TYPES = ("ftr", "dt", "sldNum")

_STRONG = re.compile(r"[A-Za-zÀ-ɏ֐-ࣿיִ-﷿ﹰ-ﻼ]")
_RTL_CHAR = re.compile(r"[֐-ࣿיִ-﷿ﹰ-ﻼ]")
_HEBREW = re.compile(r"[֐-׿]")

# a:buAutoNum schemes: (number renderer, before, after).
_AUTONUM = {
    "arabicPeriod": (str, "", "."), "arabicParenR": (str, "", ")"), "arabicParenBoth": (str, "(", ")"), "arabicPlain": (str, "", ""),
    "arabicDbPeriod": (str, "", "."), "arabicDbPlain": (str, "", ""),
    "alphaLcPeriod": (latin_letter, "", "."), "alphaLcParenR": (latin_letter, "", ")"), "alphaLcParenBoth": (latin_letter, "(", ")"),
    "alphaUcPeriod": (lambda v: latin_letter(v).upper(), "", "."), "alphaUcParenR": (lambda v: latin_letter(v).upper(), "", ")"),
    "alphaUcParenBoth": (lambda v: latin_letter(v).upper(), "(", ")"),
    "romanLcPeriod": (lambda v: roman(v).lower(), "", "."), "romanLcParenR": (lambda v: roman(v).lower(), "", ")"),
    "romanLcParenBoth": (lambda v: roman(v).lower(), "(", ")"),
    "romanUcPeriod": (roman, "", "."), "romanUcParenR": (roman, "", ")"), "romanUcParenBoth": (roman, "(", ")"),
    "hebrew2Minus": (hebrew_letter, "", "-"),
}


class PptxError(ValueError):
    pass


@dataclass
class _Item:
    """A shape on a slide, with its box in slide coordinates (EMU)."""

    element: object
    box: tuple[float, float, float, float] | None
    kind: str  # "sp", "grp", "table", "pic", "chart", "diagram", "other"
    children: list["_Item"] = field(default_factory=list)
    placeholder: str | None = None  # the placeholder's type ("body" when it has an index only)


def _xfrm(element) -> tuple[float, float, float, float] | None:
    """The box (x0, y0, x1, y1) of a shape's own transform, in its parent's coordinates."""
    for path in ("./" + P + "spPr/" + A + "xfrm", "./" + P + "grpSpPr/" + A + "xfrm", "./" + P + "xfrm"):
        xfrm = element.find(path)
        if xfrm is not None:
            off, ext = xfrm.find(A + "off"), xfrm.find(A + "ext")
            if off is not None and ext is not None:
                x, y = _num(off.get("x")), _num(off.get("y"))
                return (x, y, x + _num(ext.get("cx")), y + _num(ext.get("cy")))
    return None


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _nv(element):
    """The shape's non-visual properties (cNvPr) and its placeholder element, if any."""
    for child in element:
        if child.tag.startswith(P + "nv"):
            c_nv = child.find(P + "cNvPr")
            ph = child.find(P + "nvPr/" + P + "ph")
            return c_nv, ph
    return None, None


def _placeholder_key(ph) -> tuple[str, str | None]:
    kind = ph.get("type", "body" if ph.get("idx") is not None else "body")
    return kind, ph.get("idx")


class _Deck:
    def __init__(self, body: bytes, doc: Document) -> None:
        self.pkg = _Package(body)
        self.doc = doc
        self.repairs_on = doc.options.get("repairs", True)
        main = next((t for _, typ, t in self.pkg.rels("") if typ.endswith("/officeDocument")), "ppt/presentation.xml")
        root = self.xml(main)
        if root is None:
            raise PptxError("no presentation part")
        rels = {rid: (typ, target) for rid, typ, target in self.pkg.rels(main)}
        self.slides = []
        id_list = root.find(P + "sldIdLst")
        for sld in id_list if id_list is not None else []:
            typ, target = rels.get(sld.get(R + "id", ""), ("", ""))
            if typ.endswith("/slide"):
                self.slides.append(target)
        self.rtl_attr = (root.get("rtl") or "").lower() in ("1", "true")
        self._layout_boxes: dict[str, dict] = {}

    def read(self, name: str) -> bytes | None:
        data = self.pkg.read(name)
        if data is None:
            return None
        for strict, transitional in _STRICT:
            data = data.replace(strict, transitional)
        return data

    def xml(self, name: str | None):
        data = self.read(name) if name else None
        return ElementTree.fromstring(data) if data else None

    def rel(self, part: str, suffix: str) -> str | None:
        return next((t for _, typ, t in self.pkg.rels(part) if typ.endswith("/" + suffix)), None)

    # -- placeholder positions, inherited from the layout and the master ---------------
    def _boxes(self, part: str | None) -> dict:
        """{(type, idx): box, (type, None): box} for a layout's or master's placeholders."""
        if not part:
            return {}
        if part not in self._layout_boxes:
            boxes: dict = {}
            root = self.xml(part)
            tree = root.find(P + "cSld/" + P + "spTree") if root is not None else None
            for sp in tree.iter(P + "sp") if tree is not None else []:
                _, ph = _nv(sp)
                box = _xfrm(sp)
                if ph is None or box is None:
                    continue
                kind, idx = _placeholder_key(ph)
                boxes.setdefault((kind, idx), box)
                boxes.setdefault((kind, None), box)
                if idx is not None:
                    boxes.setdefault((None, idx), box)
            self._layout_boxes[part] = boxes
        return self._layout_boxes[part]

    def inherited_box(self, slide_part: str, ph) -> tuple | None:
        kind, idx = _placeholder_key(ph)
        layout = self.rel(slide_part, "slideLayout")
        master = self.rel(layout, "slideMaster") if layout else None
        for boxes in (self._boxes(layout), self._boxes(master)):
            for key in ((kind, idx), (None, idx), (kind, None)):
                if key in boxes and (key[0] is not None or key[1] is not None):
                    return boxes[key]
            if kind in TITLE_TYPES:
                for other in TITLE_TYPES:
                    if (other, None) in boxes:
                        return boxes[(other, None)]
        return None

    # -- one slide -------------------------------------------------------------------------
    def items(self, slide_part: str, tree, transform=None) -> list[_Item]:
        out: list[_Item] = []
        for child in tree:
            tag = child.tag
            if tag not in (P + "sp", P + "grpSp", P + "graphicFrame", P + "pic", P + "cxnSp"):
                if tag == "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent":
                    choice = child.find("{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice")
                    chosen = choice if choice is not None else child.find("{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback")
                    if chosen is not None:
                        out += self.items(slide_part, chosen, transform)
                continue
            _, ph = _nv(child)
            box = _xfrm(child)
            if box is None and ph is not None:
                box = self.inherited_box(slide_part, ph)
            if box is not None and transform is not None:
                box = transform(box)
            placeholder = _placeholder_key(ph)[0] if ph is not None else None
            if tag == P + "grpSp":
                item = _Item(child, box, "grp")
                inner = self._group_transform(child, transform)
                item.children = self.items(slide_part, child, inner)
            elif tag == P + "graphicFrame":
                data = child.find(A + "graphic/" + A + "graphicData")
                uri = data.get("uri", "") if data is not None else ""
                kind = "table" if uri.endswith(_URI_TABLE) else "chart" if uri.endswith(_URI_CHART) else "diagram" if uri.endswith(_URI_DIAGRAM) else "other"
                item = _Item(child, box, kind)
            elif tag == P + "pic":
                item = _Item(child, box, "pic")
            else:
                item = _Item(child, box, "sp", placeholder=placeholder)
            out.append(item)
        return out

    @staticmethod
    def _group_transform(group, outer):
        xfrm = group.find(P + "grpSpPr/" + A + "xfrm")
        if xfrm is None:
            return outer
        off, ext, ch_off, ch_ext = (xfrm.find(A + n) for n in ("off", "ext", "chOff", "chExt"))
        if None in (off, ext, ch_off, ch_ext):
            return outer
        ox, oy, cx, cy = _num(off.get("x")), _num(off.get("y")), _num(ext.get("cx")), _num(ext.get("cy"))
        chx, chy, chcx, chcy = _num(ch_off.get("x")), _num(ch_off.get("y")), _num(ch_ext.get("cx")) or 1, _num(ch_ext.get("cy")) or 1

        def inner(box):
            x0, y0, x1, y1 = box
            mapped = (ox + (x0 - chx) * cx / chcx, oy + (y0 - chy) * cy / chcy, ox + (x1 - chx) * cx / chcx, oy + (y1 - chy) * cy / chcy)
            return outer(mapped) if outer is not None else mapped

        return inner


def reading_order(items: list[_Item], rtl: bool) -> list[_Item]:
    """Titles first; then rows top to bottom, each right to left in an RTL deck."""
    titles = [i for i in items if i.placeholder in TITLE_TYPES]
    placed = [i for i in items if i.box is not None and i not in titles]
    unplaced = [i for i in items if i.box is None and i not in titles]
    placed.sort(key=lambda i: (i.box[1], i.box[3]))
    rows: list[list[_Item]] = []
    for item in placed:
        if rows:
            row = rows[-1]
            top, bottom = min(i.box[1] for i in row), max(i.box[3] for i in row)
            overlap = min(bottom, item.box[3]) - max(top, item.box[1])
            smaller = min(bottom - top, item.box[3] - item.box[1]) or 1
            if overlap >= ROW_OVERLAP * smaller:
                row.append(item)
                continue
        rows.append([item])
    ordered = list(titles)
    for row in rows:
        ordered += sorted(row, key=lambda i: -i.box[2] if rtl else i.box[0])
    return ordered + unplaced


@dataclass
class _Para:
    text: str
    level: int
    rtl: bool | None
    bullet: tuple | None  # ("char", "•") / ("auto", scheme, start) / None
    flags: list[str] = field(default_factory=list)


def _paragraphs(body) -> list[_Para]:
    out = []
    for p in body.findall(A + "p"):
        ppr = p.find(A + "pPr")
        pieces = []
        for child in p:
            if child.tag in (A + "r", A + "fld"):
                t = child.find(A + "t")
                pieces.append(t.text or "" if t is not None else "")
            elif child.tag == A + "br":
                pieces.append("\n")
        bullet = None
        rtl = None
        level = 0
        if ppr is not None:
            rtl = ppr.get("rtl").lower() in ("1", "true") if ppr.get("rtl") is not None else None
            level = int(ppr.get("lvl", "0")) if (ppr.get("lvl") or "0").isdigit() else 0
            if ppr.find(A + "buNone") is not None:
                bullet = None
            elif ppr.find(A + "buAutoNum") is not None:
                auto = ppr.find(A + "buAutoNum")
                start = auto.get("startAt", "1")
                bullet = ("auto", auto.get("type", "arabicPeriod"), int(start) if start.isdigit() else 1)
            elif ppr.find(A + "buChar") is not None:
                bullet = ("char", ppr.find(A + "buChar").get("char", "•"))
        out.append(_Para("".join(pieces), level, rtl, bullet))
    return out


class _SlideReader:
    def __init__(self, deck: _Deck, number: int, part: str, rtl: bool) -> None:
        self.deck = deck
        self.doc = deck.doc
        self.number = number
        self.part = part
        self.rtl = rtl
        self.part_rels = {rid: target for rid, _, target in deck.pkg.rels(part)}

    def direction(self, text: str, rtl: bool | None) -> str | None:
        if rtl is not None:
            return "rtl" if rtl else "ltr"
        strong = _STRONG.search(text)
        if strong is None:
            return None
        return "rtl" if _RTL_CHAR.match(strong.group()) else "ltr"

    def text_blocks(self, body, role: str, *, heading: bool = False, flags=()) -> list[Block]:
        out = []
        counters: dict[int, int] = {}
        for para in _paragraphs(body):
            text = para.text
            label = None
            extra = list(flags)
            block_repairs: list[dict] = []
            if para.bullet and para.bullet[0] == "auto":
                _, scheme, start = para.bullet
                counters[para.level] = counters.get(para.level, start - 1) + 1
                for deeper in [k for k in counters if k > para.level]:
                    del counters[deeper]
                render, before, after = _AUTONUM.get(scheme, (str, "", "."))
                if scheme not in _AUTONUM:
                    extra.append(f"numbering_format_unsupported:{scheme}")
                label = f"{before}{render(counters[para.level])}{after}"
            else:
                counters.pop(para.level, None)
                if para.bullet and para.bullet[0] == "char":
                    label = para.bullet[1]
                    if self.deck.repairs_on:
                        label, changes = repairs.symbol_font_bullet(label)
                        if changes:
                            block_repairs += repairs.record(repairs.SYMBOL_FONT_BULLET, changes)
                            extra.append("bullet_symbol_mapped")
            if not text.strip():
                continue
            if self.deck.repairs_on:
                text, marks = repairs.direction_marks(text)
                block_repairs += repairs.record(repairs.DIRECTION_MARKS, marks)
            kind = "heading" if heading else ("list_item" if label else "text")
            out.append(
                Block(
                    kind=kind,
                    text=text,
                    role=role,
                    source="office_xml",
                    tool=TOOL,
                    level=1 if heading else None,
                    label=None if heading else label,
                    direction=self.direction(text, para.rtl),
                    slide=self.number,
                    repairs=block_repairs,
                    flags=extra,
                )
            )
        return out

    def read(self) -> list[Block]:
        root = self.deck.xml(self.part)
        if root is None:
            self.doc.unread.append(Unread(scope="block", reason="slide_missing", detail=f"slide {self.number}: its part is missing"))
            return []
        hidden = (root.get("show") or "1").lower() in ("0", "false")
        tree = root.find(P + "cSld/" + P + "spTree")
        blocks = self.items(self.deck.items(self.part, tree) if tree is not None else [])
        blocks += self.notes()
        for block in blocks:
            if hidden:
                block.flags.append("hidden_slide")
        return blocks

    def items(self, items: list[_Item]) -> list[Block]:
        out: list[Block] = []
        for item in reading_order(items, self.rtl):
            c_nv, _ = _nv(item.element)
            hidden = c_nv is not None and (c_nv.get("hidden") or "").lower() in ("1", "true")
            before = len(out)
            if item.kind == "grp":
                out += self.items(item.children)
            elif item.kind == "sp":
                body = item.element.find(P + "txBody")
                if body is not None:
                    role = "footer" if item.placeholder in FOOTER_TYPES else "body"
                    out += self.text_blocks(body, role, heading=item.placeholder in TITLE_TYPES)
            elif item.kind == "table":
                table = self.table(item.element)
                if table is not None:
                    out.append(table)
            elif item.kind == "pic":
                out += self.picture(item.element)
            elif item.kind == "diagram":
                out += self.diagram(item.element)
            elif item.kind == "chart":
                self.doc.unread.append(Unread(scope="block", reason="chart_not_read", detail=f"slide {self.number}"))
            else:
                self.doc.unread.append(Unread(scope="block", reason="embedded_content_not_read", detail=f"slide {self.number}: an embedded object (OLE or media)"))
            if hidden:
                for block in out[before:]:
                    block.flags.append("hidden_shape")
        return out

    def table(self, frame) -> Block | None:
        tbl = frame.find(A + "graphic/" + A + "graphicData/" + A + "tbl")
        if tbl is None:
            return None
        tblpr = tbl.find(A + "tblPr")
        rtl = tblpr is not None and (tblpr.get("rtl") or "").lower() in ("1", "true")
        rows: list[list[Cell]] = []
        table_repairs: Counter = Counter()
        for tr in tbl.findall(A + "tr"):
            row: list[Cell] = []
            for tc in tr.findall(A + "tc"):
                if (tc.get("hMerge") or "").lower() in ("1", "true"):
                    continue  # covered by the gridSpan to its left
                body = tc.find(A + "txBody")
                lines = []
                for block in self.text_blocks(body, "body") if body is not None else []:
                    for entry in block.repairs:
                        table_repairs[(entry["rule"], entry["from"], entry["to"])] += entry["count"]
                    lines.append(f"{block.label} {block.text}" if block.label else block.text)
                text = "\n".join(lines)
                if (tc.get("vMerge") or "").lower() in ("1", "true"):
                    span = int(tc.get("gridSpan", "1")) if (tc.get("gridSpan") or "1").isdigit() else 1
                    row.append(Cell("", colspan=max(span, 1), flags=["merged_above"] + (["merged_cell_has_text"] if text.strip() else [])))
                    continue
                colspan = int(tc.get("gridSpan", "1")) if (tc.get("gridSpan") or "1").isdigit() else 1
                rowspan = int(tc.get("rowSpan", "1")) if (tc.get("rowSpan") or "1").isdigit() else 1
                row.append(Cell(text, colspan=max(colspan, 1), rowspan=max(rowspan, 1)))
            rows.append(row)
        if not any(c.text.strip() for row in rows for c in row):
            return None
        return Block(
            kind="table",
            cells=rows,
            source="office_xml",
            tool=TOOL,
            direction="rtl" if rtl else "ltr",
            slide=self.number,
            repairs=[{"rule": r, "from": a, "to": b, "count": n} for (r, a, b), n in sorted(table_repairs.items())],
            flags=["table_ltr_layout"] if not rtl and any(_HEBREW.search(c.text) for row in rows for c in row) else [],
        )

    def picture(self, pic) -> list[Block]:
        blip = pic.find(P + "blipFill/" + A + "blip")
        rid = blip.get(R + "embed") if blip is not None else None
        target = self.part_rels.get(rid or "")
        data = self.deck.pkg.read(target) if target else None
        if data is None:
            self.doc.unread.append(Unread(scope="block", reason="image_not_read", detail=f"slide {self.number}: a picture without a readable part (linked or missing)"))
            return [Block(kind="figure", source="office_xml", tool=TOOL, slide=self.number, flags=["image_not_read"])]
        blocks = image_blocks(self.doc, data, "body", None, source="office_xml", tool=TOOL)
        for block in blocks:
            block.slide = self.number
        return blocks

    def diagram(self, frame) -> list[Block]:
        ids = frame.find(A + "graphic/" + A + "graphicData/" + DGM + "relIds")
        target = self.part_rels.get(ids.get(R + "dm", "")) if ids is not None else None
        root = self.deck.xml(target) if target else None
        if root is None:
            self.doc.unread.append(Unread(scope="block", reason="smartart_not_read", detail=f"slide {self.number}: its data part is missing"))
            return []
        out = []
        for point in root.iter(DGM + "pt"):
            if point.get("type", "node") not in ("node", ""):
                continue
            body = point.find(DGM + "t")
            if body is not None:
                out += self.text_blocks(body, "body", flags=["from_smartart"])
        return out

    def notes(self) -> list[Block]:
        part = self.deck.rel(self.part, "notesSlide")
        root = self.deck.xml(part)
        if root is None:
            return []
        out = []
        tree = root.find(P + "cSld/" + P + "spTree")
        for sp in tree.iter(P + "sp") if tree is not None else []:
            _, ph = _nv(sp)
            if ph is None or ph.get("type", "body") != "body":
                continue  # the slide image, the slide number and the header and footer of the notes page
            body = sp.find(P + "txBody")
            if body is not None:
                out += self.text_blocks(body, "notes")
        return out


def _deck_is_rtl(deck: _Deck) -> bool:
    """The presentation's rtl attribute, else most paragraphs: rtl="1", or Hebrew without a setting."""
    if deck.rtl_attr:
        return True
    votes = Counter()
    for part in deck.slides[:50]:
        root = deck.xml(part)
        if root is None:
            continue
        for p in root.iter(A + "p"):
            ppr = p.find(A + "pPr")
            text = "".join(t.text or "" for t in p.iter(A + "t"))
            if ppr is not None and ppr.get("rtl") is not None:
                votes[ppr.get("rtl").lower() in ("1", "true")] += 1
            elif text.strip():
                strong = _STRONG.search(text)
                if strong:
                    votes[bool(_RTL_CHAR.match(strong.group()))] += 1
    return votes[True] > votes[False]


def read_pptx(body: bytes, doc: Document) -> None:
    """Fill ``doc`` with the slides of a PPTX presentation."""
    doc.components["defusedxml"] = defusedxml.__version__
    try:
        deck = _Deck(body, doc)
        rtl = _deck_is_rtl(deck)
        doc._rtl = rtl
        count = page_limit(doc, len(deck.slides))
        doc.page_count = None
        blocks: list[Block] = []
        for number, part in enumerate(deck.slides[:count], 1):
            blocks += _SlideReader(deck, number, part, rtl).read()
    except (zipfile.BadZipFile, DocxError, PptxError, ElementTree.ParseError, defusedxml.DefusedXmlException) as exc:
        doc.unread.append(Unread(scope="document", reason="pptx_unreadable", detail=f"{type(exc).__name__}: {exc}"[:300]))
        doc.flags.append("not_read")
        return
    doc.blocks.extend(blocks)
    doc.flags.append("deck_rtl" if rtl else "deck_ltr")
    if any("hidden_slide" in b.flags for b in blocks):
        doc.flags.append("hidden_slides_read")
    if not any(b.role == "body" for b in blocks):
        doc.flags.append("no_text")
