"""A ``Document`` written as a Word file (DOCX), for readers who open documents in Word.

The file is a minimal OOXML package written with :mod:`zipfile` and our own XML: no new dependency.
It holds what ``Document.text`` holds, in the same order, laid out the way a Word user expects:

- **Paragraphs** right to left (``w:bidi``), with right-to-left runs (``w:rtl``) for Hebrew and Arabic
  script, and Arial, which has Hebrew letters on Windows, macOS and in Google Docs, at 12 pt for both
  Latin and complex script (``w:sz``, ``w:szCs``). Line breaks in a block become ``w:br``, tabs ``w:tab``.
- **Headings** in the built-in styles Heading 1 to Heading 6 (deeper levels in Heading 6), so Word's
  navigation pane and table of contents find them.
- **List items** with their labels, as read ("1.1.", "(א)"), written as text: a label Word numbered
  itself could come out different from the one that was read.
- **Tables** as Word tables, right to left (``w:bidiVisual``) when the table is; spans as
  ``w:gridSpan`` and ``w:vMerge``.
- **Pages:** a page break between a PDF's pages (or a deck's slides), on the first paragraph of the
  next page (``w:pageBreakBefore``).
- **Notes, comments, headers and footers** in their own sections after the body, each under its title,
  starting on a new page. The titles are Hebrew when most of the body's letters are Hebrew
  (``HEBREW_TITLES``), else English, as in ``Document.text``.

Figures that were not read leave no trace in the body, as in ``Document.text``. The package is
byte-stable: the same record gives the same file.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

from heocr.contract import MAX_SPAN
# The roles in the body flow, and the sections after it with their titles, as ``Document.text`` has them.
from heocr.render import _INLINE_ROLES, _SECTIONS

if TYPE_CHECKING:  # pragma: no cover
    from heocr.contract import Block, Cell, Document

FONT = "Arial"
SIZE = 24  # half-points: 12 pt
HEADING_SIZES = (36, 32, 28, 26, 24, 24)  # Heading 1..6, half-points
TEXT_WIDTH = 9026  # twips: A4 (11906) less two 2.54 cm margins (1440 each)
LIST_INDENT = 360  # twips

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml"

_RTL_LETTER = re.compile(r"[֐-ࣿיִ-﷿ﹰ-﻿]")
_LTR_LETTER = re.compile(r"[A-Za-zÀ-ɏͰ-ϿЀ-ӿ]")
# Characters XML 1.0 cannot carry (C0 controls but tab, line feed and carriage return; lone surrogates; U+FFFE/F).
_NOT_XML = re.compile(r"[\x00-\x08\x0b-\x1f\ud800-\udfff￾￿]")

# Section titles for a document whose body is mostly Hebrew: the file is for people, who read it in
# the document's language. ``Document.text`` keeps the English titles.
HEBREW_TITLES = {
    "footnote": "הערות שוליים",
    "endnote": "הערות סיום",
    "comment": "הערות",
    "header": "כותרות עליונות",
    "footer": "כותרות תחתונות",
    "notes": "הערות דובר",
    "navigation": "ניווט",
}
_HEBREW_LETTER = re.compile(r"[\u05d0-\u05ea]")

_FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # zip entries carry no write time: the same record gives the same bytes


def _clean(text: str) -> str:
    return _NOT_XML.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _is_rtl(text: str, direction: str | None, default: bool) -> bool:
    if direction in ("rtl", "ltr"):
        return direction == "rtl"
    if _RTL_LETTER.search(text):
        return True
    if _LTR_LETTER.search(text):
        return False
    return default


# -- runs ----------------------------------------------------------------------


def _script_runs(text: str, rtl_paragraph: bool) -> list[tuple[str, bool]]:
    """``text`` cut where the script changes: (piece, right to left).

    Digits after Latin letters go with them ("QX-418"), as the bidi algorithm reads them. Spaces and
    punctuation between two pieces of one script stay inside; between two scripts, or at the end, they
    take the paragraph's direction, so a sentence's last full stop stays at its end. In a right-to-left
    piece, digits and punctuation stay in it."""
    out: list[list] = []  # [piece, rtl]
    pending = ""  # neutrals not yet given to a side
    for ch in text:
        if _RTL_LETTER.match(ch):
            strong = True
        elif _LTR_LETTER.match(ch) or (ch.isdigit() and out and out[-1][1] is False):
            strong = False
        else:
            pending += ch
            continue
        if out and out[-1][1] == strong:
            out[-1][0] += pending + ch
        elif out and pending and rtl_paragraph and out[-1][1] is False:
            # Between a Latin piece and a Hebrew one, in a right-to-left paragraph: the neutrals go right to left.
            out.append([pending + ch, True])
        elif out:
            out[-1][0] += pending
            out.append([ch, strong])
        else:
            out.append([pending + ch, strong])
        pending = ""
    if pending:
        if out and out[-1][1] is False and rtl_paragraph:
            out.append([pending, True])
        elif out:
            out[-1][0] += pending
        else:
            out.append([pending, rtl_paragraph])
    return [(piece, bool(rtl)) for piece, rtl in out]


def _run_content(text: str) -> str:
    parts = []
    for i, line in enumerate(text.split("\n")):
        if i:
            parts.append("<w:br/>")
        for j, chunk in enumerate(line.split("\t")):
            if j:
                parts.append("<w:tab/>")
            if chunk:
                parts.append(f'<w:t xml:space="preserve">{escape(chunk)}</w:t>')
    return "".join(parts)


def _runs(text: str, rtl_paragraph: bool, bold: bool = False) -> str:
    out = []
    for piece, rtl in _script_runs(_clean(text), rtl_paragraph):
        props = []
        if bold:
            props.append("<w:b/><w:bCs/>")
        if rtl:
            props.append("<w:rtl/>")
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        out.append(f"<w:r>{rpr}{_run_content(piece)}</w:r>")
    return "".join(out)


def _paragraph(text: str, rtl: bool, *, style: str | None = None, page_break: bool = False, indent: int = 0, bold: bool = False) -> str:
    ppr = []
    if style:
        ppr.append(f"<w:pStyle w:val={quoteattr(style)}/>")
    if page_break:
        ppr.append("<w:pageBreakBefore/>")
    # Always said, either way: a paragraph style or a reader's default must not turn it.
    ppr.append("<w:bidi/>" if rtl else '<w:bidi w:val="0"/>')
    if indent:
        ppr.append(f'<w:ind w:start="{indent}"/>')
    return f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{_runs(text, rtl, bold)}</w:p>"


# -- tables --------------------------------------------------------------------


def _grid(rows: list[list["Cell"]]) -> list[list[tuple[str, "Cell | None", int]]]:
    """Each row as Word cells: ("cell", cell, span), or ("continue", cell or None, span) for a cell
    merged with the one above. A record lists a merged cell either again (DOCX: flagged
    ``merged_above``) or not at all (HTML, PDF): both become a ``w:vMerge`` continuation."""
    covered: dict[int, list[int]] = {}  # column -> [rows still covered, span]
    out = []
    for row in rows:
        cells = []
        column = 0
        pending = list(row)

        def cover_here():
            nonlocal column
            rows_left, span = covered[column]
            here = column
            column += span
            if rows_left <= 1:
                del covered[here]
            else:
                covered[here][0] -= 1
            return span

        while pending or any(c >= column for c in covered):
            if column in covered:
                if pending and "merged_above" in pending[0].flags:
                    cell = pending.pop(0)
                    cover_here()
                    cells.append(("continue", cell, min(max(cell.colspan, 1), MAX_SPAN)))
                else:
                    cells.append(("continue", None, cover_here()))
                continue
            if not pending:
                # A covered column further on, with no cell before it: an empty cell keeps the grid.
                nxt = min(c for c in covered if c >= column)
                cells.append(("cell", None, nxt - column))
                column = nxt
                continue
            cell = pending.pop(0)
            span = min(max(cell.colspan, 1), MAX_SPAN)
            if "merged_above" in cell.flags:
                cells.append(("continue", cell, span))
            else:
                cells.append(("cell", cell, span))
                rowspan = min(max(cell.rowspan, 1), MAX_SPAN)
                if rowspan > 1:
                    covered[column] = [rowspan - 1, span]
            column += span
        out.append(cells)
    return out


def _cell_xml(kind: str, cell: "Cell | None", span: int, width: int, rtl: bool, restart: bool) -> str:
    tcpr = [f'<w:tcW w:w="{width * span}" w:type="dxa"/>']
    if span > 1:
        tcpr.append(f'<w:gridSpan w:val="{span}"/>')
    if kind == "continue":
        tcpr.append("<w:vMerge/>")
    elif restart:
        tcpr.append('<w:vMerge w:val="restart"/>')
    text = cell.text if cell is not None and kind == "cell" else ""
    paragraphs = "".join(_paragraph(line, _is_rtl(line, None, rtl)) for line in text.split("\n"))
    return f"<w:tc><w:tcPr>{''.join(tcpr)}</w:tcPr>{paragraphs}</w:tc>"


def _table(block: "Block", default_rtl: bool) -> str:
    rows = _grid(block.cells or [])
    if not rows or not any(rows):
        return ""
    texts = " ".join(c.text for row in block.cells or [] for c in row)
    rtl = _is_rtl(texts, block.direction, default_rtl)
    columns = max(sum(span for _, _, span in row) for row in rows)
    columns = max(columns, 1)
    width = max(TEXT_WIDTH // columns, 1)
    tblpr = '<w:tblStyle w:val="TableGrid"/>' + ("<w:bidiVisual/>" if rtl else "") + '<w:tblW w:w="0" w:type="auto"/><w:tblLook w:val="04A0"/>'
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _ in range(columns))
    body = []
    for row in rows:
        cells = []
        used = 0
        for kind, cell, span in row:
            restart = kind == "cell" and cell is not None and min(max(cell.rowspan, 1), MAX_SPAN) > 1
            cells.append(_cell_xml(kind, cell, span, width, rtl, restart))
            used += span
        if used < columns:  # a short row: empty cells fill it, so every row has the grid's columns
            cells.append(_cell_xml("cell", None, columns - used, width, rtl, False))
        body.append(f"<w:tr>{''.join(cells)}</w:tr>")
    return f"<w:tbl><w:tblPr>{tblpr}</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{''.join(body)}</w:tbl>"


# -- blocks --------------------------------------------------------------------


def _labelled(block: "Block") -> str:
    if not block.label:
        return block.text
    joiner = "" if "label_suffix_nothing" in block.flags else " "
    return f"{block.label}{joiner}{block.text}"


def _block(block: "Block", default_rtl: bool, page_break: bool, prefix: str = "") -> list[str]:
    """The block's XML; a page break before it, if asked, on its first paragraph."""
    if block.kind == "figure":
        return []
    if block.kind == "table":
        table = _table(block, default_rtl)
        if not table:
            return []
        # A table cannot carry the break: an empty paragraph does (heocr's reader drops empty paragraphs).
        return ([_paragraph("", default_rtl, page_break=True)] if page_break else []) + [table]
    text = prefix + _labelled(block)
    if not text:
        return []
    rtl = _is_rtl(text, block.direction, default_rtl)
    if block.kind == "heading":
        level = min(max(block.level or 1, 1), 6)
        return [_paragraph(text, rtl, style=f"Heading{level}", page_break=page_break)]
    indent = LIST_INDENT if block.kind == "list_item" else 0
    return [_paragraph(text, rtl, page_break=page_break, indent=indent)]


def _document_rtl(doc: "Document") -> bool:
    """The document's direction: right to left unless it has Latin letters and no Hebrew or Arabic ones."""
    texts = [b.text for b in doc.blocks] + [c.text for b in doc.blocks for row in (b.cells or []) for c in row]
    joined = " ".join(texts)
    return bool(_RTL_LETTER.search(joined)) or not _LTR_LETTER.search(joined)


def _body(doc: "Document") -> str:
    rtl = _document_rtl(doc)
    parts: list[str] = []
    previous: int | None = None
    page_break = False
    for block in doc.blocks:
        if block.role not in _INLINE_ROLES:
            continue
        where = block.page if block.page is not None else block.slide
        if previous is not None and where is not None and where != previous:
            page_break = True
        previous = where if where is not None else previous
        # The break waits for a block that writes something: a figure or an empty block does not take it.
        xml = _block(block, rtl, page_break and bool(parts))
        if xml:
            parts.extend(xml)
            page_break = False
    return _with_sections(doc, rtl, parts)


def _mostly_hebrew(doc: "Document") -> bool:
    """Whether most letters of the body are Hebrew letters."""
    texts = []
    for block in doc.blocks:
        if block.role in _INLINE_ROLES:
            texts.append(block.text)
            texts.extend(c.text for row in (block.cells or []) for c in row)
    joined = " ".join(texts)
    hebrew = len(_HEBREW_LETTER.findall(joined))
    return hebrew > 0 and hebrew * 2 > hebrew + len(_LTR_LETTER.findall(joined))


def _with_sections(doc: "Document", rtl: bool, parts: list[str]) -> str:
    first = True
    hebrew = _mostly_hebrew(doc)
    for role, title in _SECTIONS:
        if hebrew:
            title = HEBREW_TITLES[role]
        items = [b for b in doc.blocks if b.role == role]
        written: list[str] = []
        for block in items:
            prefix = f"[{block.note_id}] " if block.note_id and block.kind != "table" else ""
            written.extend(_block(block, rtl, False, prefix))
        if written:
            # The sections start on a page of their own, after the body, and follow one another on it.
            parts.append(_paragraph(title, rtl, style="heocrSection", page_break=first and bool(parts)))
            parts.extend(written)
            first = False
    sect = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>'
        + ("<w:bidi/>" if rtl else "")
        + "</w:sectPr>"
    )
    if not parts:
        parts.append(_paragraph("", rtl))
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{_W}" xmlns:r="{_R}"><w:body>{"".join(parts)}{sect}</w:body></w:document>'
    )


# -- package parts -------------------------------------------------------------


def _styles() -> str:
    rfonts = f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:eastAsia="{FONT}" w:cs="{FONT}"/>'
    lang = '<w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="he-IL"/>'
    headings = []
    for level, size in enumerate(HEADING_SIZES, start=1):
        headings.append(
            f'<w:style w:type="paragraph" w:styleId="Heading{level}"><w:name w:val="heading {level}"/>'
            '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>'
            f'<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="{level - 1}"/></w:pPr>'
            f'<w:rPr><w:b/><w:bCs/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:styles xmlns:w="{_W}">'
        f"<w:docDefaults><w:rPrDefault><w:rPr>{rfonts}"
        f'<w:sz w:val="{SIZE}"/><w:szCs w:val="{SIZE}"/>{lang}</w:rPr></w:rPrDefault>'
        f'<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
        + "".join(headings)
        + '<w:style w:type="paragraph" w:customStyle="1" w:styleId="heocrSection"><w:name w:val="heocr section"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/></w:pPr>'
        '<w:rPr><w:b/><w:bCs/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>'
        '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/>'
        '<w:semiHidden/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
        '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
        '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/>'
        '<w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:tblPr><w:tblBorders>'
        + "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="auto"/>' for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
        + "</w:tblBorders></w:tblPr></w:style>"
        "</w:styles>"
    )


def _settings() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:settings xmlns:w="{_W}"><w:defaultTabStop w:val="720"/><w:characterSpacingControl w:val="doNotCompress"/>'
        '<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
        "</w:settings>"
    )


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Types xmlns="{_CT}">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/word/document.xml" ContentType="{_MAIN}.document.main+xml"/>'
    f'<Override PartName="/word/styles.xml" ContentType="{_MAIN}.styles+xml"/>'
    f'<Override PartName="/word/settings.xml" ContentType="{_MAIN}.settings+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Relationships xmlns="{_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Relationships xmlns="{_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_REL}/styles" Target="styles.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL}/settings" Target="settings.xml"/>'
    "</Relationships>"
)


def parts(doc: "Document") -> dict[str, str]:
    """The package's parts, by name, in the order they are written."""
    return {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _ROOT_RELS,
        "word/document.xml": _body(doc),
        "word/_rels/document.xml.rels": _DOCUMENT_RELS,
        "word/styles.xml": _styles(),
        "word/settings.xml": _settings(),
    }


def to_docx(doc: "Document") -> bytes:
    """The document as the bytes of a DOCX file."""
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        for name, xml in parts(doc).items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            package.writestr(info, xml.encode("utf-8"))
    return out.getvalue()
