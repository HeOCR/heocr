"""Synthetic XLSX workbooks, written part by part (no spreadsheet library)."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def c(ref: str, value=None, *, t: str | None = None, s: int | None = None, f: str | None = None, inline: str | None = None) -> str:
    """One cell. ``value`` goes in <v>; ``inline`` makes an inline string."""
    attrs = f' r="{ref}"' + (f' t="{t}"' if t else "") + (f' s="{s}"' if s is not None else "")
    if inline is not None:
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(inline)}</t></is></c>'
    inner = (f"<f>{escape(f)}</f>" if f else "") + (f"<v>{escape(str(value))}</v>" if value is not None else "")
    return f"<c{attrs}>{inner}</c>"


def row(r: int, cells: list[str], *, hidden: bool = False) -> str:
    return f'<row r="{r}"' + (' hidden="1"' if hidden else "") + ">" + "".join(cells) + "</row>"


@dataclass
class Sheet:
    name: str
    rows: list[str] = field(default_factory=list)
    rtl: bool = True
    merges: list[str] = field(default_factory=list)
    hidden_cols: list[tuple[int, int]] = field(default_factory=list)
    state: str | None = None
    header: str | None = None
    footer: str | None = None
    comments: dict[str, str] = field(default_factory=dict)
    drawing: bool = False

    def xml(self) -> str:
        cols = "".join(f'<col min="{a}" max="{b}" hidden="1"/>' for a, b in self.hidden_cols)
        parts = [
            f"<worksheet {NS}>",
            f'<sheetViews><sheetView workbookViewId="0"' + (' rightToLeft="1"' if self.rtl else "") + "/></sheetViews>",
            f"<cols>{cols}</cols>" if cols else "",
            "<sheetData>" + "".join(self.rows) + "</sheetData>",
            ("<mergeCells>" + "".join(f'<mergeCell ref="{m}"/>' for m in self.merges) + "</mergeCells>") if self.merges else "",
            ("<headerFooter>" + (f"<oddHeader>{escape(self.header)}</oddHeader>" if self.header else "") + (f"<oddFooter>{escape(self.footer)}</oddFooter>" if self.footer else "") + "</headerFooter>")
            if self.header or self.footer
            else "",
            '<drawing r:id="rIdD"/>' if self.drawing else "",
            '<legacyDrawing r:id="rIdV"/>' if self.comments else "",
            "</worksheet>",
        ]
        return "".join(parts)


@dataclass
class Workbook:
    sheets: list[Sheet] = field(default_factory=list)
    strings: list[str] = field(default_factory=list)
    num_fmts: dict[int, str] = field(default_factory=dict)  # custom id -> format code
    xfs: list[int] = field(default_factory=lambda: [0])  # cellXfs: numFmtId per style index
    date1904: bool = False
    chart_sheets: list[str] = field(default_factory=list)
    strict: bool = False

    def build(self) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            ct = "application/vnd.openxmlformats-officedocument.spreadsheetml"
            overrides = [("/xl/workbook.xml", f"{ct}.sheet.main+xml"), ("/xl/sharedStrings.xml", f"{ct}.sharedStrings+xml"), ("/xl/styles.xml", f"{ct}.styles+xml")]
            overrides += [(f"/xl/worksheets/sheet{i}.xml", f"{ct}.worksheet+xml") for i in range(1, len(self.sheets) + 1)]
            overrides += [(f"/xl/comments{i}.xml", f"{ct}.comments+xml") for i, sh in enumerate(self.sheets, 1) if sh.comments]
            z.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                + "".join(f'<Override PartName="{name}" ContentType="{kind}"/>' for name, kind in overrides)
                + "</Types>",
            )
            z.writestr("_rels/.rels", f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}officeDocument" Target="xl/workbook.xml"/></Relationships>')
            rels, sheets = [], []
            n = 0
            for i, sheet in enumerate(self.sheets, 1):
                n += 1
                rels.append(f'<Relationship Id="rId{n}" Type="{REL}worksheet" Target="worksheets/sheet{i}.xml"/>')
                state = f' state="{sheet.state}"' if sheet.state else ""
                sheets.append(f'<sheet name="{escape(sheet.name)}" sheetId="{n}"{state} r:id="rId{n}"/>')
                z.writestr(f"xl/worksheets/sheet{i}.xml", self._ns(sheet.xml()))
                sheet_rels = []
                if sheet.comments:
                    body = "".join(f'<comment ref="{ref}" authorId="0"><text><r><t>{escape(text)}</t></r></text></comment>' for ref, text in sheet.comments.items())
                    z.writestr(f"xl/comments{i}.xml", f"<comments {NS}><authors><author>a</author></authors><commentList>{body}</commentList></comments>")
                    sheet_rels.append(f'<Relationship Id="rIdC" Type="{REL}comments" Target="../comments{i}.xml"/>')
                if sheet.drawing:
                    sheet_rels.append(f'<Relationship Id="rIdD" Type="{REL}drawing" Target="../drawings/drawing{i}.xml"/>')
                if sheet_rels:
                    z.writestr(f"xl/worksheets/_rels/sheet{i}.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(sheet_rels) + "</Relationships>")
            for j, name in enumerate(self.chart_sheets, 1):
                n += 1
                rels.append(f'<Relationship Id="rId{n}" Type="{REL}chartsheet" Target="chartsheets/sheet{j}.xml"/>')
                sheets.append(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>')
            n += 1
            rels.append(f'<Relationship Id="rId{n}" Type="{REL}sharedStrings" Target="sharedStrings.xml"/>')
            n += 1
            rels.append(f'<Relationship Id="rId{n}" Type="{REL}styles" Target="styles.xml"/>')
            pr = '<workbookPr date1904="1"/>' if self.date1904 else ""
            z.writestr("xl/workbook.xml", self._ns(f"<workbook {NS}>{pr}<sheets>{''.join(sheets)}</sheets></workbook>"))
            z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(rels) + "</Relationships>")
            sst = "".join(f"<si><t>{escape(s)}</t></si>" if isinstance(s, str) else s[0] for s in self.strings)
            z.writestr("xl/sharedStrings.xml", self._ns(f'<sst {NS} count="{len(self.strings)}">{sst}</sst>'))
            fmts = "".join(f'<numFmt numFmtId="{i}" formatCode="{escape(code, {chr(34): "&quot;"})}"/>' for i, code in self.num_fmts.items())
            xfs = "".join(f'<xf numFmtId="{i}"/>' for i in self.xfs)
            z.writestr("xl/styles.xml", self._ns(f"<styleSheet {NS}><numFmts>{fmts}</numFmts><cellXfs>{xfs}</cellXfs></styleSheet>"))
        return out.getvalue()

    def _ns(self, xml: str) -> str:
        if not self.strict:
            return xml
        return xml.replace("http://schemas.openxmlformats.org/spreadsheetml/2006/main", "http://purl.oclc.org/ooxml/spreadsheetml/main")
