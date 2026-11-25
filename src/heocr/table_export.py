"""A document's tables as data files: one CSV per table, or one XLSX workbook with a sheet per table (D3g).

Each table is written 1 to 1 as the record holds it:

- **Positions:** the logical grid, column 0 first. That is the start column: the rightmost one in a right-to-left
  table. Rows are padded to the table's width, so every row has the same number of fields.
- **Spans:** a merged cell's text sits at its top-left (start) position. In CSV the positions it covers are empty
  strings. In XLSX they are a real merge (``mergeCells``).
- **Text** is written exactly as read, always as a string: never parsed as a number or date, so "007", "1,250" and
  "12.3.1980" stay as printed. Line breaks inside a cell are kept; characters XML cannot hold are dropped (XLSX only).
- **Direction:** a right-to-left table's sheet is shown right to left (``sheetView rightToLeft``), so column A,
  logical column 0, is on the right, as on the page.

Both writers use :mod:`csv` and :mod:`zipfile` with our own XML: no new dependency. The XLSX package is byte-stable:
the same record gives the same file.
"""
from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import replace
import io
import zipfile
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

from heocr.docx.writer import _FIXED_TIME, _clean, _grid, _is_rtl

if TYPE_CHECKING:  # pragma: no cover
    from heocr.contract import Block, Document


def tables(doc: "Document") -> list["Block"]:
    """Return physical tables or their verified logical joins in reading order."""
    blocks = [b for b in doc.blocks if b.kind == "table" and b.cells]
    if not doc.options.get("join_tables"):
        return blocks
    groups = defaultdict(list)
    output = []
    for block in blocks:
        link = (block.checks or {}).get("table_continuation")
        if link is None:
            output.append(block)
            continue
        key = link["group"]
        if not groups[key]:
            output.append(key)
        groups[key].append((link["part"], link["repeated_header_rows"], block))
    joined = {}
    for key, parts in groups.items():
        parts.sort(key=lambda part: part[0])
        cells = [row for _, repeated, block in parts for row in block.cells[repeated:]]
        joined[key] = replace(parts[0][2], cells=cells)
    return [joined[entry] if isinstance(entry, str) else entry for entry in output]


def table_grid(block: "Block") -> tuple[list[list[str]], list[tuple[int, int, int, int]]]:
    """The table as a full grid of strings (logical columns, start first) and its merges as (row, col, rowspan,
    colspan), each covering more than one position."""
    rows = _grid(block.cells or [])
    width = max((sum(span for _, _, span in row) for row in rows), default=0)
    grid = [[""] * width for _ in rows]
    merges = []
    for r, row in enumerate(rows):
        col = 0
        for kind, cell, span in row:
            if kind == "cell" and cell is not None:
                grid[r][col] = cell.text
                rowspan = min(max(cell.rowspan, 1), len(rows) - r)
                if span > 1 or rowspan > 1:
                    merges.append((r, col, rowspan, span))
            col += span
    return grid, merges


def to_csv(block: "Block") -> str:
    """One table as CSV (RFC 4180: comma separated, CRLF line ends, quoted where needed)."""
    grid, _ = table_grid(block)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerows(grid)
    return out.getvalue()


def _column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _ref(row: int, col: int) -> str:
    return f"{_column(col)}{row + 1}"


def _sheet(block: "Block", default_rtl: bool) -> str:
    grid, merges = table_grid(block)
    rtl = _is_rtl(" ".join(" ".join(row) for row in grid), block.direction, default_rtl)
    rows = []
    for r, row in enumerate(grid):
        cells = []
        for c, text in enumerate(row):
            if not text:
                continue
            value = escape(_clean(text))
            cells.append(f'<c r="{_ref(r, c)}" t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>')
        rows.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
    view = '<sheetView workbookViewId="0"' + (' rightToLeft="1"' if rtl else "") + "/>"
    merged = ""
    if merges:
        refs = "".join(f'<mergeCell ref="{_ref(r, c)}:{_ref(r + rs - 1, c + cs - 1)}"/>' for r, c, rs, cs in merges)
        merged = f'<mergeCells count="{len(merges)}">{refs}</mergeCells>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetViews>{view}</sheetViews><sheetData>{''.join(rows)}</sheetData>{merged}</worksheet>")


def _sheet_name(block: "Block", number: int) -> str:
    """'Table 3' or 'Table 3 p12' (page from 1); at most 31 characters, as Excel allows."""
    name = f"Table {number}" + (f" p{block.page + 1}" if block.page is not None else "")
    return name[:31]


def to_xlsx(doc: "Document") -> bytes:
    """Every table in ``doc`` as a sheet of one workbook (an empty workbook has one empty sheet)."""
    blocks = tables(doc)
    default_rtl = bool(getattr(doc, "_rtl", True))
    sheets = [(_sheet_name(b, i + 1), _sheet(b, default_rtl)) for i, b in enumerate(blocks)]
    if not sheets:
        sheets = [("Table 1", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   "<sheetData/></worksheet>")]
    ns = "http://schemas.openxmlformats.org"
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Types xmlns="{ns}/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(len(sheets)))
            + "</Types>"),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<workbook xmlns="{ns}/spreadsheetml/2006/main" xmlns:r="{ns}/officeDocument/2006/relationships">'
            "<sheets>"
            + "".join(f"<sheet name={quoteattr(name)} sheetId=\"{i + 1}\" r:id=\"rId{i + 1}\"/>"
                      for i, (name, _) in enumerate(sheets))
            + "</sheets></workbook>"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="{ns}/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
            + "</Relationships>"),
    }
    for i, (_, xml) in enumerate(sheets):
        parts[f"xl/worksheets/sheet{i + 1}.xml"] = xml
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        for name, xml in parts.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, xml.encode("utf-8"))
    return out.getvalue()
