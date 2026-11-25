"""Table export (D3g): each table as CSV, all tables as one XLSX workbook, read back by heocr's own XLSX reader.

All text here is invented for the tests.
"""

import contextlib
import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from defusedxml import ElementTree

import heocr
from heocr.cli import main
from heocr.contract import Block, Cell, Document
from heocr.table_export import table_grid, to_csv

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _doc() -> Document:
    doc = Document(sha256="0" * 64, size=1, format="pdf", format_evidence="test", page_count=2)
    doc.blocks = [
        Block(kind="text", text="לפני הטבלה", page=0, direction="rtl"),
        Block(kind="table", page=0, direction="rtl", cells=[
            [Cell("שם", colspan=2), Cell("סכום", rowspan=2)],
            [Cell("א"), Cell("שורה\nשנייה"), Cell("", flags=["merged_above"])],
            [Cell("ב"), Cell("007"), Cell("1,250")],
        ]),
        Block(kind="table", page=1, direction="ltr", cells=[
            [Cell("Code"), Cell("Date")],
            [Cell("TR-905"), Cell("12.3.1980")],
        ]),
    ]
    return doc


def _read_back(data: bytes) -> Document:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "back.xlsx"
        path.write_bytes(data)
        return heocr.extract(path, ocr=False, convert=False)


class GridTest(unittest.TestCase):
    def test_spans_give_a_full_grid_and_merges(self) -> None:
        grid, merges = table_grid(_doc().blocks[1])
        self.assertEqual(grid, [["שם", "", "סכום"], ["א", "שורה\nשנייה", ""], ["ב", "007", "1,250"]])
        self.assertEqual(merges, [(0, 0, 1, 2), (0, 2, 2, 1)])

    def test_a_rowspan_listed_without_placeholders_keeps_the_grid(self) -> None:
        block = Block(kind="table", cells=[[Cell("א", rowspan=2), Cell("ב")], [Cell("ג")]])
        grid, merges = table_grid(block)
        self.assertEqual(grid, [["א", "ב"], ["", "ג"]])
        self.assertEqual(merges, [(0, 0, 2, 1)])

    def test_short_rows_are_padded(self) -> None:
        grid, _ = table_grid(Block(kind="table", cells=[[Cell("א"), Cell("ב"), Cell("ג")], [Cell("ד")]]))
        self.assertEqual(grid, [["א", "ב", "ג"], ["ד", "", ""]])


class CsvTest(unittest.TestCase):
    def test_csv_is_the_grid_as_text(self) -> None:
        text = to_csv(_doc().blocks[1])
        self.assertTrue(text.endswith("\r\n"))
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(rows, [["שם", "", "סכום"], ["א", "שורה\nשנייה", ""], ["ב", "007", "1,250"]])
        self.assertEqual(len(_doc().tables_csv()), 2)


class XlsxTest(unittest.TestCase):
    def test_workbook_reads_back_cell_for_cell(self) -> None:
        data = _doc().to_xlsx()
        self.assertEqual(data, _doc().to_xlsx())  # byte-stable
        back = _read_back(data)
        tables = [b for b in back.blocks if b.kind == "table"]
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0].direction, "rtl")
        # The reader's record: a spanning cell once, with its spans (the record's own convention, as written).
        self.assertEqual([[c.text for c in row] for row in tables[0].cells],
                         [["שם", "סכום"], ["א", "שורה\nשנייה", ""], ["ב", "007", "1,250"]])
        self.assertEqual((tables[0].cells[0][0].colspan, tables[0].cells[0][1].rowspan), (2, 2))
        self.assertEqual(table_grid(tables[0]), table_grid(_doc().blocks[1]))
        self.assertEqual([[c.text for c in row] for row in tables[1].cells], [["Code", "Date"], ["TR-905", "12.3.1980"]])
        self.assertNotEqual(tables[1].direction, "rtl")

    def test_sheets_are_named_by_table_and_page(self) -> None:
        with zipfile.ZipFile(io.BytesIO(_doc().to_xlsx())) as package:
            workbook = ElementTree.fromstring(package.read("xl/workbook.xml"))
            sheet = ElementTree.fromstring(package.read("xl/worksheets/sheet1.xml"))
        self.assertEqual([s.get("name") for s in workbook.iter(f"{S}sheet")], ["Table 1 p1", "Table 2 p2"])
        self.assertEqual([m.get("ref") for m in sheet.iter(f"{S}mergeCell")], ["A1:B1", "C1:C2"])
        # Values are strings, never reparsed as numbers or dates.
        self.assertEqual({c.get("t") for c in sheet.iter(f"{S}c")}, {"inlineStr"})

    def test_no_tables_gives_one_empty_sheet(self) -> None:
        doc = Document(sha256="0" * 64, size=1, format="pdf", format_evidence="test")
        self.assertEqual([b for b in _read_back(doc.to_xlsx()).blocks if b.kind == "table"], [])


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.input = self.dir / "in.xlsx"
        self.input.write_bytes(_doc().to_xlsx())

    def test_xlsx_by_suffix(self) -> None:
        out = self.dir / "out.xlsx"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--no-ocr", "-o", str(out)]), 0)
        self.assertEqual(len([b for b in _read_back(out.read_bytes()).blocks if b.kind == "table"]), 2)

    def test_csv_writes_one_file_per_table(self) -> None:
        out = self.dir / "t.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--no-ocr", "-o", str(out)]), 0)
        self.assertFalse(out.exists())
        first = (self.dir / "t-1.csv").read_bytes().decode("utf-8")
        self.assertTrue(first.startswith("שם,,סכום\r\n"))
        self.assertTrue((self.dir / "t-2.csv").exists())

    def test_csv_to_standard_output_keeps_crlf(self) -> None:
        buffer = io.BytesIO()
        stdout = mock.Mock(buffer=buffer, isatty=lambda: False)
        with mock.patch("sys.stdout", stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--no-ocr", "-f", "csv"]), 0)
        text = buffer.getvalue().decode("utf-8")
        self.assertTrue(text.startswith("שם,,סכום\r\n"))
        self.assertNotIn("\r\r", text)
        self.assertIn("\r\n\r\nCode,Date\r\n", text)  # tables separated by an empty line


if __name__ == "__main__":
    unittest.main()
