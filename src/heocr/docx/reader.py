"""The DOCX route: our own reading of WordprocessingML, with numbering rendered.

The volunteer's Word-based pipeline served as the specification for what to keep
(AdanimInstitute/social-procurement-yonatan-amster-project, scripts/extract_compare.py:
``mark_headings``, ``delete_hidden_text``, ``mark_cell_breaks``, ``remove_invisible_text``).
This module does the same without Word: headings from outline levels, TOC lines not
taken as headings, hidden text dropped, white text without a background dropped, and
line breaks in table cells kept.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

import defusedxml
from defusedxml import ElementTree

from heocr.contract import Block, Cell, Document, Unread
from heocr.docx.numbering import W, Numbering
from heocr.docx.styles import Styles, toggle

TOOL = "heocr.docx"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
STRICT_W = b"http://purl.oclc.org/ooxml/wordprocessingml/main"
TRANSITIONAL_W = b"http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"

MAX_MEMBERS = 10_000
MAX_PART_BYTES = 256 * 1024**2
MAX_EXPANDED_BYTES = 1024**3

_LETTER = re.compile(r"[^\W\d_]")
_IMAGE_TAGS = {
    "{http://schemas.openxmlformats.org/drawingml/2006/main}blip",
    "{urn:schemas-microsoft-com:vml}imagedata",
}
_TEXTBOX = W + "txbxContent"


class DocxError(ValueError):
    pass


class _Package:
    def __init__(self, body: bytes) -> None:
        self.zip = zipfile.ZipFile(io.BytesIO(body))
        infos = self.zip.infolist()
        if len(infos) > MAX_MEMBERS:
            raise DocxError(f"{len(infos)} ZIP members; the limit is {MAX_MEMBERS}")
        if sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
            raise DocxError("expanded size above the limit")
        self.names = {i.filename: i for i in infos}

    def read(self, name: str) -> bytes | None:
        info = self.names.get(name)
        if info is None:
            return None
        if info.file_size > MAX_PART_BYTES:
            raise DocxError(f"part {name} is above the size limit")
        data = self.zip.read(info)
        return data.replace(STRICT_W, TRANSITIONAL_W)

    def rels(self, part: str) -> list[tuple[str, str, str]]:
        """(id, type, target part) for a part's relationships."""
        folder, base = posixpath.split(part)
        xml = self.read(posixpath.join(folder, "_rels", base + ".rels"))
        if not xml:
            return []
        out = []
        for rel in ElementTree.fromstring(xml).findall(PKG_REL + "Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
            out.append((rel.get("Id", ""), rel.get("Type", ""), resolved))
        return out


@dataclass
class _Paragraph:
    text: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    visible_chars: int = 0
    hidden_chars: int = 0
    white_chars: int = 0
    figures: int = 0
    textboxes: list = field(default_factory=list)


class _Reader:
    def __init__(self, pkg: _Package, doc: Document) -> None:
        self.pkg = pkg
        self.doc = doc
        main = next((t for _, typ, t in pkg.rels("") if typ.endswith("/officeDocument")), "word/document.xml")
        self.main = main
        self.rels = pkg.rels(main)
        self.styles = Styles.parse(self._part("styles"))
        self.numbering = Numbering.parse(self._part("numbering"))

    def _part(self, rel_type: str) -> bytes | None:
        for _, typ, target in self.rels:
            if typ == REL_TYPE + rel_type:
                return self.pkg.read(target)
        return None

    # -- tree walking ---------------------------------------------------------
    def _children(self, element):
        """Children with markup-compatibility choices resolved (first Choice, never Fallback)."""
        for child in element:
            if child.tag == MC + "AlternateContent":
                choice = child.find(MC + "Choice")
                chosen = choice if choice is not None else child.find(MC + "Fallback")
                if chosen is not None:
                    yield from self._children(chosen)
            else:
                yield child

    def blocks(self, container, role: str, note_id: str | None = None, shaded: bool = False) -> list[Block]:
        out: list[Block] = []
        for child in self._children(container):
            tag = child.tag
            if tag == W + "p":
                out.extend(self.paragraph(child, role, note_id, shaded))
            elif tag == W + "tbl":
                out.append(self.table(child, role, note_id))
            elif tag in (W + "sdt",):
                content = child.find(W + "sdtContent")
                if content is not None:
                    out.extend(self.blocks(content, role, note_id, shaded))
            elif tag in (W + "customXml", W + "ins", W + "moveTo"):
                out.extend(self.blocks(child, role, note_id, shaded))
            elif tag == W + "altChunk":
                self.doc.unread.append(Unread(scope="block", reason="docx_altchunk_not_read", detail="embedded foreign content (altChunk)"))
        return out

    # -- paragraphs -----------------------------------------------------------
    def _shaded(self, props) -> bool:
        if props is None:
            return False
        shd = props.find(W + "shd")
        if shd is not None and (shd.get(W + "fill") or "auto").upper() not in ("AUTO", "FFFFFF"):
            return True
        highlight = props.find(W + "highlight")
        return highlight is not None and highlight.get(W + "val", "none") != "none"

    def _runs(self, element, pstyle: str | None, para: _Paragraph, shaded: bool, fields: list[str]) -> None:
        for child in self._children(element):
            tag = child.tag
            if tag == W + "r":
                self._run(child, pstyle, para, shaded, fields)
            elif tag in (W + "hyperlink", W + "smartTag", W + "customXml", W + "ins", W + "moveTo", W + "fldSimple", W + "dir", W + "bdo"):
                self._runs(child, pstyle, para, shaded, fields)
            elif tag == W + "sdt":
                content = child.find(W + "sdtContent")
                if content is not None:
                    self._runs(content, pstyle, para, shaded, fields)
            elif tag in (W + "del", W + "moveFrom"):
                continue
            elif tag == "{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath" or tag.endswith("}oMathPara"):
                para.flags.append("math_not_read")

    def _run(self, run, pstyle: str | None, para: _Paragraph, shaded: bool, fields: list[str]) -> None:
        rpr = run.find(W + "rPr")
        rstyle_el = rpr.find(W + "rStyle") if rpr is not None else None
        rstyle = rstyle_el.get(W + "val") if rstyle_el is not None else None
        hidden = self.styles.hidden(rpr, rstyle, pstyle)
        white = not shaded and not self._shaded(rpr) and self.styles.white(rpr, rstyle, pstyle)
        pieces: list[str] = []
        for child in self._children(run):
            tag = child.tag
            if tag == W + "fldChar":
                kind = child.get(W + "fldCharType")
                if kind == "begin":
                    fields.append("instr")
                elif kind == "separate" and fields:
                    fields[-1] = "result"
                elif kind == "end" and fields:
                    fields.pop()
                continue
            if fields and fields[-1] == "instr":
                continue  # field code, not displayed
            if tag == W + "t":
                pieces.append(child.text or "")
            elif tag == W + "tab" or tag == W + "ptab":
                pieces.append("\t")
            elif tag in (W + "br", W + "cr"):
                pieces.append("\n")
            elif tag == W + "noBreakHyphen":
                pieces.append("-")
            elif tag == W + "sym":
                code = child.get(W + "char", "")
                try:
                    pieces.append(chr(int(code, 16)))
                    para.flags.append("symbol_font_char")
                except ValueError:
                    para.flags.append("symbol_unreadable")
            elif tag in (W + "drawing", W + "pict", W + "object"):
                self._drawing(child, para)
            elif tag in (W + "footnoteReference", W + "endnoteReference"):
                para.flags.append("has_note_reference")
        text = "".join(pieces)
        if not text:
            return
        if hidden:
            para.hidden_chars += len(text)
            para.flags.append("hidden_text_dropped")
            return
        if white and text.strip():
            para.hidden_chars += len(text)
            para.white_chars += len(text)
            para.flags.append("white_text_dropped")
            return
        para.text.append(text)
        para.visible_chars += len(text.strip())

    def _drawing(self, element, para: _Paragraph) -> None:
        boxes = [e for e in element.iter(_TEXTBOX)]
        if boxes:
            para.textboxes.extend(boxes)
        elif any(e.tag in _IMAGE_TAGS for e in element.iter()):
            para.figures += 1

    def _label(self, ppr, pstyle: str | None):
        """The paragraph's list label. ``numId`` and ``ilvl`` resolve field by field:
        the paragraph's own ``numPr``, then the nearest style that sets each one; with no
        ``ilvl`` anywhere, the level whose ``pStyle`` names a style in the chain, else 0."""
        num_el = ppr.find(W + "numPr") if ppr is not None else None
        num_id, ilvl = None, None
        if num_el is not None:
            n, i = num_el.find(W + "numId"), num_el.find(W + "ilvl")
            num_id = n.get(W + "val") if n is not None else None
            ilvl = int(i.get(W + "val", "0")) if i is not None and (i.get(W + "val") or "").isdigit() else None
        style_num, style_ilvl, chain = self.styles.num_pr(pstyle)
        if num_id is None:
            num_id = style_num
        if ilvl is None:
            ilvl = style_ilvl
        if num_id is None or num_id == "0":
            return None
        if not self.numbering.knows(num_id):
            self.doc.flags.append("numbering_reference_unknown")
            return None
        if ilvl is None:
            for style_id in chain or ([pstyle] if pstyle else []):
                ilvl = self.numbering.level_for_style(num_id, style_id)
                if ilvl is not None:
                    break
        return self.numbering.next_label(num_id, ilvl or 0)

    def paragraph(self, p, role: str, note_id: str | None, shaded: bool) -> list[Block]:
        ppr = p.find(W + "pPr")
        style_el = ppr.find(W + "pStyle") if ppr is not None else None
        pstyle = style_el.get(W + "val") if style_el is not None else None
        para = _Paragraph()
        self._runs(p, pstyle, para, shaded or self._shaded(ppr), [])
        text = "".join(para.text)
        mark_rpr = ppr.find(W + "rPr") if ppr is not None else None
        fully_hidden = not text.strip() and para.hidden_chars > 0
        if fully_hidden or (not text and toggle(mark_rpr, "vanish")):
            # A paragraph with only hidden text does not show, and is not numbered here.
            white = para.hidden_chars and para.white_chars == para.hidden_chars
            self.doc.flags.append("white_paragraph_dropped" if white else "hidden_paragraph_dropped")
            return []
        label = self._label(ppr, pstyle)
        out: list[Block] = []
        bidi = toggle(ppr, "bidi") if ppr is not None else None
        if bidi is None:
            bidi = self.styles.bidi(pstyle)
        direction = "rtl" if bidi else "ltr"

        outline = None
        if ppr is not None and ppr.find(W + "outlineLvl") is not None:
            try:
                outline = int(ppr.find(W + "outlineLvl").get(W + "val", "9"))
            except ValueError:
                outline = None
        if outline is None:
            outline = self.styles.outline_level(pstyle)
        heading = (
            outline is not None
            and 0 <= outline <= 8
            and not self.styles.is_toc(pstyle)
            and _LETTER.search(text) is not None
        )
        if text or label is not None:
            flags = list(para.flags)
            if label is not None:
                flags.extend(label.flags)
                if label.suffix == "nothing":
                    flags.append("label_suffix_nothing")
            kind = "heading" if heading else ("list_item" if label is not None and label.text else "text")
            out.append(
                Block(
                    kind=kind,
                    text=text,
                    role=role,
                    source="office_xml",
                    tool=TOOL,
                    level=(outline + 1) if heading else None,
                    label=label.text if label is not None and label.text else None,
                    note_id=note_id,
                    direction=direction,
                    flags=flags,
                )
            )
        for box in para.textboxes:
            for block in self.blocks(box, "textbox" if role == "body" else role, note_id):
                block.flags.append("from_text_box")
                out.append(block)
        for _ in range(para.figures):
            out.append(Block(kind="figure", role=role, source="office_xml", tool=TOOL, note_id=note_id, flags=["image_not_read"]))
            self.doc.unread.append(Unread(scope="block", reason="image_not_read", detail="embedded image; OCR route arrives in D2"))
        return out

    # -- tables ---------------------------------------------------------------
    def _cell_text(self, tc) -> tuple[str, list[str]]:
        tcpr = tc.find(W + "tcPr")
        shaded = self._shaded(tcpr)
        lines: list[str] = []
        flags: list[str] = []
        for block in self.blocks(tc, "body", shaded=shaded):
            if block.kind == "table":
                flags.append("nested_table")
                lines.extend(" | ".join(c.text.replace("\n", " ") for c in row) for row in block.cells or [])
            elif block.kind == "figure":
                flags.append("image_not_read")
            else:
                text = block.text
                if block.label:
                    joiner = "" if "label_suffix_nothing" in block.flags else " "
                    text = f"{block.label}{joiner}{text}"
                lines.append(text)
            flags.extend(f for f in block.flags if f in ("hidden_text_dropped", "white_text_dropped"))
        return "\n".join(lines), flags

    def table(self, tbl, role: str, note_id: str | None) -> Block:
        tblpr = tbl.find(W + "tblPr")
        rtl = bool(toggle(tblpr, "bidiVisual"))
        rows: list[list[Cell]] = []
        open_merges: dict[int, Cell] = {}
        flags: list[str] = []
        for tr in self._children(tbl):
            if tr.tag != W + "tr":
                continue
            row: list[Cell] = []
            column = 0
            trpr = tr.find(W + "trPr")
            before = trpr.find(W + "gridBefore") if trpr is not None else None
            if before is not None and (before.get(W + "val") or "").isdigit():
                column += int(before.get(W + "val"))
            for tc in self._children(tr):
                if tc.tag == W + "sdt":
                    content = tc.find(W + "sdtContent")
                    cells = [c for c in content if c.tag == W + "tc"] if content is not None else []
                elif tc.tag == W + "tc":
                    cells = [tc]
                else:
                    continue
                for cell_el in cells:
                    tcpr = cell_el.find(W + "tcPr")
                    span_el = tcpr.find(W + "gridSpan") if tcpr is not None else None
                    span = int(span_el.get(W + "val")) if span_el is not None and (span_el.get(W + "val") or "").isdigit() else 1
                    vmerge = tcpr.find(W + "vMerge") if tcpr is not None else None
                    text, cell_flags = self._cell_text(cell_el)
                    cell = Cell(text=text, colspan=max(span, 1), flags=cell_flags)
                    if vmerge is not None and vmerge.get(W + "val", "continue") == "continue" and column in open_merges:
                        open_merges[column].rowspan += 1
                        cell.flags.append("merged_above")
                        if text.strip():
                            cell.flags.append("merged_cell_has_text")
                    elif vmerge is not None and vmerge.get(W + "val") == "restart":
                        open_merges[column] = cell
                    else:
                        open_merges.pop(column, None)
                    row.append(cell)
                    column += max(span, 1)
            rows.append(row)
        if not rtl and any(re.search(r"[֐-׿]", c.text) for row in rows for c in row):
            flags.append("table_ltr_layout")
        return Block(
            kind="table",
            cells=rows,
            role=role,
            source="office_xml",
            tool=TOOL,
            note_id=note_id,
            direction="rtl" if rtl else "ltr",
            flags=flags,
        )

    # -- stories --------------------------------------------------------------
    def body(self) -> list[Block]:
        xml = self.pkg.read(self.main)
        if xml is None:
            raise DocxError("no main document part")
        root = ElementTree.fromstring(xml)
        body = root.find(W + "body")
        return self.blocks(body, "body") if body is not None else []

    def notes(self, rel_type: str, tag: str, role: str) -> list[Block]:
        xml = self._part(rel_type)
        if not xml:
            return []
        out: list[Block] = []
        for note in ElementTree.fromstring(xml).findall(W + tag):
            if note.get(W + "type") in ("separator", "continuationSeparator", "continuationNotice"):
                continue
            out.extend(self.blocks(note, role, note_id=note.get(W + "id")))
        return out

    def headers_footers(self) -> list[Block]:
        out: list[Block] = []
        seen: set[str] = set()
        for kind in ("header", "footer"):
            for _, typ, target in self.rels:
                if typ != REL_TYPE + kind or target in seen:
                    continue
                seen.add(target)
                xml = self.pkg.read(target)
                if xml:
                    out.extend(self.blocks(ElementTree.fromstring(xml), kind))
        return out


def read_docx(body: bytes, doc: Document) -> None:
    """Fill ``doc`` with the blocks of a DOCX file."""
    doc.components["defusedxml"] = defusedxml.__version__
    try:
        pkg = _Package(body)
        reader = _Reader(pkg, doc)
        blocks = reader.body()
        blocks += reader.notes("footnotes", "footnote", "footnote")
        blocks += reader.notes("endnotes", "endnote", "endnote")
        blocks += reader.notes("comments", "comment", "comment")
        blocks += reader.headers_footers()
    except (zipfile.BadZipFile, DocxError, ElementTree.ParseError, defusedxml.DefusedXmlException) as exc:
        doc.unread.append(Unread(scope="document", reason="docx_unreadable", detail=f"{type(exc).__name__}: {exc}"[:300]))
        doc.flags.append("not_read")
        return
    doc.blocks.extend(blocks)
    if not blocks:
        doc.flags.append("no_text")
