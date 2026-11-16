"""D4d: XLSX workbooks, each sheet a table block. Synthetic workbooks only."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from heocr import xlsx
from tests.synth.xlsx import Sheet, Workbook, c, row


def extract(book: Workbook, **kwargs) -> heocr.Document:
    doc = heocr.extract(book.build(), **kwargs)
    assert heocr.validate(doc.to_dict()) == [], heocr.validate(doc.to_dict())
    return doc


def grid(block):
    return [[cell.text for cell in r] for r in block.cells]


def tables(doc):
    return [b for b in doc.blocks if b.kind == "table"]


# Styles: 0 General, 1 #,##0.00, 2 0%, 3 short date (14), 4 custom ₪ amount, 5 custom Hebrew long date, 6 fraction (unknown), 7 time.
STYLED = Workbook(
    strings=["שם", "סכום", "תאריך", "אחוז"],
    num_fmts={164: '#,##0 "₪"', 165: "[$-40D]d mmmm yyyy", 166: "# ?/?"},
    xfs=[0, 4, 9, 14, 164, 165, 166, 20],
)


class SheetTest(unittest.TestCase):
    def test_a_sheet_is_a_heading_and_a_table(self) -> None:
        book = Workbook(strings=["שם", "כמות", "עט", "מחברת"], sheets=[
            Sheet("מלאי", rows=[row(1, [c("A1", 0, t="s"), c("B1", 1, t="s")]), row(2, [c("A2", 2, t="s"), c("B2", 5)]), row(3, [c("A3", 3, t="s"), c("B3", 12)])]),
        ])
        doc = extract(book)
        self.assertEqual(doc.format, "xlsx")
        self.assertEqual([(b.kind, b.text) for b in doc.blocks if b.kind == "heading"], [("heading", "מלאי")])
        self.assertIn("sheet_name", doc.blocks[0].flags)
        table = tables(doc)[0]
        self.assertEqual(grid(table), [["שם", "כמות"], ["עט", "5"], ["מחברת", "12"]])
        self.assertEqual(table.direction, "rtl")
        self.assertEqual({b.source for b in doc.blocks}, {"office_xml"})
        self.assertEqual({b.tool for b in doc.blocks}, {"heocr.xlsx"})
        self.assertEqual(doc.unread, [])

    def test_sheets_in_order_and_direction_per_sheet(self) -> None:
        book = Workbook(sheets=[Sheet("Summary", rows=[row(1, [c("A1", inline="Total")])], rtl=False), Sheet("פירוט", rows=[row(1, [c("A1", inline="שורה")])])])
        doc = extract(book)
        self.assertEqual([b.text for b in doc.blocks if b.kind == "heading"], ["Summary", "פירוט"])
        self.assertEqual([t.direction for t in tables(doc)], ["ltr", "rtl"])

    def test_used_range_starts_at_the_first_value_and_keeps_gaps(self) -> None:
        book = Workbook(sheets=[Sheet("א", rows=[row(3, [c("C3", 1), c("E3", 2)]), row(5, [c("D5", 3)])])])
        self.assertEqual(grid(tables(extract(book))[0]), [["1", "", "2"], ["", "", ""], ["", "3", ""]])

    def test_strict_namespace_and_cells_without_references(self) -> None:
        book = Workbook(strict=True, sheets=[Sheet("א", rows=['<row><c><v>1</v></c><c><v>2</v></c></row><row><c><v>3</v></c></row>'])])
        self.assertEqual(grid(tables(extract(book))[0]), [["1", "2"], ["3", ""]])

    def test_an_empty_workbook_and_a_broken_one(self) -> None:
        empty = extract(Workbook(sheets=[Sheet("ריק")]))
        self.assertIn("no_text", empty.flags)
        self.assertEqual(tables(empty), [])
        doc = heocr.extract(Workbook(sheets=[Sheet("א", rows=["<row"])]).build())
        self.assertEqual([u.reason for u in doc.unread], ["xlsx_unreadable"])
        self.assertIn("not_read", doc.flags)


class ValueTest(unittest.TestCase):
    def test_strings_formulas_booleans_and_errors(self) -> None:
        rich = ('<si><r><t>טקסט </t></r><r><rPr><b/></rPr><t>מודגש</t></r><rPh><t>פונטי</t></rPh></si>',)
        book = Workbook(strings=["רגיל", rich, "x_x000D_y"], sheets=[Sheet("א", rows=[row(1, [
            c("A1", 0, t="s"), c("B1", 1, t="s"), c("C1", inline="בתוך התא"), c("D1", 2, t="s"),
        ]), row(2, [
            c("A2", 30, f="SUM(B3:B4)"), c("B2", "מחושב", t="str", f="A1&\"\""), c("C2", f="NOW()"), c("D2", 1, t="b"), c("E2", "#DIV/0!", t="e", f="1/0"),
        ])])])
        table = tables(extract(book))[0]
        self.assertEqual(grid(table), [["רגיל", "טקסט מודגש", "בתוך התא", "x\ry", ""], ["30", "מחושב", "", "TRUE", "#DIV/0!"]])
        flags = {cell.text: cell.flags for cell in table.cells[1]}
        self.assertEqual(table.cells[1][2].flags, ["formula_not_cached"])
        self.assertEqual(flags["#DIV/0!"], ["formula_error"])

    def test_numbers_and_dates_as_their_format_shows_them(self) -> None:
        book = Workbook(**{**STYLED.__dict__, "sheets": [Sheet("א", rows=[row(1, [
            c("A1", "1234.5"), c("B1", "1234.567", s=1), c("C1", "0.155", s=2), c("D1", "46342", s=3),
            c("E1", "8500000", s=4), c("F1", "46342", s=5), c("G1", "0.75", s=7), c("H1", "0.30000000000000004"),
        ])])]})
        doc = extract(book)
        self.assertEqual(grid(tables(doc)[0]), [["1234.5", "1,234.57", "16%", "16/11/2026", "8,500,000 ₪", "16 נובמבר 2026", "18:00", "0.3"]])
        self.assertIn("date_format_locale_assumed", doc.flags)

    def test_an_unknown_format_keeps_the_raw_value_and_says_so(self) -> None:
        book = Workbook(**{**STYLED.__dict__, "sheets": [Sheet("א", rows=[row(1, [c("A1", "1.25", s=6)])])]})
        cell = tables(extract(book))[0].cells[0][0]
        self.assertEqual((cell.text, cell.flags), ("1.25", ["number_format_unknown"]))

    def test_the_1904_date_system(self) -> None:
        book = Workbook(date1904=True, xfs=[0, 14], sheets=[Sheet("א", rows=[row(1, [c("A1", "44880", s=1)])])])
        self.assertEqual(grid(tables(extract(book))[0]), [["16/11/2026"]])


class FormatTest(unittest.TestCase):
    def test_codes(self) -> None:
        cases = [
            (1234.5, "General", "1234.5"), (1.5e-7, "General", "1.5E-07"), (2.5, "0", "3"), (-1234, "#,##0 ;(#,##0)", "(1,234)"),
            (-5, "0.00", "-5.00"), (0, '0;-0;"-"', "-"), (0.5, "#.00", ".50"), (5, "000", "005"), (3, '0 "ימים"', "3 ימים"),
            (8500000, "[$₪-40D] #,##0.00", "₪ 8,500,000.00"), (1234.5, "#,##0.00_);[Red](#,##0.00)", "1,234.50 "),
            (46342, "yyyy-mm-dd", "2026-11-16"), (46342.75, "dd/mm/yyyy h:mm", "16/11/2026 18:00"), (46342, "d-mmm-yy", "16-Nov-26"),
            (0.5, "h:mm AM/PM", "12:00 PM"), (61, "dd/mm/yyyy", "01/03/1900"), (46342, "dddd", "Monday"),
        ]
        for value, code, expected in cases:
            with self.subTest(code=code, value=value):
                self.assertEqual(xlsx.format_number(value, code), expected)

    def test_codes_beyond_the_renderer(self) -> None:
        for value, code in ((1.25, "# ?/?"), (12345678, "#,##0,"), (5, "[>100]0;0"), (1.5, "[h]:mm"), (1e5, "0.00E+00"), (0.5, "dd/mm/yyyy"), (60, "dd/mm/yyyy")):
            with self.subTest(code=code):
                with self.assertRaises(xlsx._Unknown):
                    xlsx.format_number(value, code)


class MergeAndHiddenTest(unittest.TestCase):
    def test_merged_cells_are_spans(self) -> None:
        book = Workbook(sheets=[Sheet("א", merges=["A1:C1", "A2:A3", "B2:C3"], rows=[
            row(1, [c("A1", inline="כותרת")]), row(2, [c("A2", inline="קבוצה"), c("B2", inline="גוש")]), row(3, [c("A3", inline="נסתר בתוך מיזוג")]), row(4, [c("A4", 1), c("B4", 2), c("C4", 3)]),
        ])])
        cells = [[(x.text, x.colspan, x.rowspan, x.flags) for x in r] for r in tables(extract(book))[0].cells]
        self.assertEqual(cells, [
            [("כותרת", 3, 1, [])],
            [("קבוצה", 1, 2, []), ("גוש", 2, 2, [])],
            [("", 1, 1, ["merged_above", "merged_cell_has_text"]), ("", 2, 1, ["merged_above"])],
            [("1", 1, 1, []), ("2", 1, 1, []), ("3", 1, 1, [])],
        ])

    def test_hidden_sheets_rows_and_columns_are_read_and_flagged(self) -> None:
        book = Workbook(sheets=[
            Sheet("גלוי", hidden_cols=[(2, 2)], rows=[row(1, [c("A1", 1), c("B1", 2), c("C1", 3)]), row(2, [c("A2", 4), c("B2", 5), c("C2", 6)], hidden=True)]),
            Sheet("סמוי", state="hidden", rows=[row(1, [c("A1", inline="סוד")])]),
            Sheet("סמוי מאוד", state="veryHidden", rows=[row(1, [c("A1", inline="סוד עמוק")])]),
        ])
        doc = extract(book)
        first, second, third = tables(doc)
        self.assertEqual(grid(first), [["1", "2", "3"], ["4", "5", "6"]])
        self.assertEqual(first.cells[0][1].flags, ["hidden_column"])
        self.assertEqual(first.cells[1][0].flags, ["hidden_row"])
        self.assertEqual(first.cells[1][1].flags, ["hidden_row", "hidden_column"])
        self.assertEqual(set(first.flags), {"hidden_rows_read", "hidden_columns_read"})
        self.assertEqual([grid(second), grid(third)], [[["סוד"]], [["סוד עמוק"]]])
        self.assertTrue(all("hidden_sheet" in t.flags for t in (second, third)))
        self.assertIn("hidden_sheets_read", doc.flags)


class AroundTheGridTest(unittest.TestCase):
    def test_comments_headers_footers_drawings_and_chart_sheets(self) -> None:
        book = Workbook(chart_sheets=["גרף"], sheets=[Sheet(
            "א", rows=[row(1, [c("A1", 1)])], comments={"A1": "לבדוק את הסכום"}, drawing=True,
            header="&L&\"Arial,Bold\"משרד האוצר&C&14מכרז 5/2026&R&D", footer="&Cעמוד &P מתוך &N",
        )])
        doc = extract(book)
        comments = [b for b in doc.blocks if b.role == "comment"]
        self.assertEqual([(b.text, b.note_id) for b in comments], [("לבדוק את הסכום", "א!A1")])
        self.assertEqual([b.text for b in doc.blocks if b.role == "header"], ["משרד האוצר  מכרז 5/2026"])
        self.assertEqual([b.text for b in doc.blocks if b.role == "footer"], ["עמוד  מתוך"])
        self.assertEqual(sorted(u.reason for u in doc.unread), ["chart_not_read", "xlsx_drawing_not_read"])
        body, _, rest = doc.text.partition("--- Comments ---")
        self.assertNotIn("לבדוק", body)

    def test_a_sheet_beyond_the_cell_limit_is_cut_and_says_so(self) -> None:
        rows = [row(r, [c(f"A{r}", r), c(f"B{r}", r)]) for r in range(1, 21)]
        with mock.patch.object(xlsx, "MAX_SHEET_CELLS", 10):
            doc = extract(Workbook(sheets=[Sheet("א", rows=rows)]))
        self.assertEqual(len(tables(doc)[0].cells), 5)
        self.assertEqual([u.reason for u in doc.unread], ["sheet_truncated"])
        self.assertIn("rows 6-20", doc.unread[0].detail)


@unittest.skipUnless(os.environ.get("HEOCR_TEST_SOFFICE") and shutil.which("soffice"), "opt-in; needs LibreOffice")
class LibreOfficeXlsxTest(unittest.TestCase):
    """A synthetic workbook saved again by real LibreOffice reads back the same."""

    def test_libreoffice_roundtrip(self) -> None:
        book = Workbook(**{**STYLED.__dict__, "sheets": [Sheet("נתונים", merges=["A1:B1"], rows=[
            row(1, [c("A1", inline="כותרת ממוזגת")]), row(2, [c("A2", 0, t="s"), c("B2", 1, t="s")]),
            row(3, [c("A3", inline="עט"), c("B3", "1234.567", s=1)]), row(4, [c("A4", inline="תאריך"), c("B4", "46342", s=3)]),
        ])]})
        expected = grid(tables(extract(book))[0])
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in" / "book.xlsx"
            source.parent.mkdir()
            source.write_bytes(book.build())
            subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--convert-to", "xlsx", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            doc = heocr.extract(Path(tmp) / "book.xlsx")
        self.assertEqual(grid(tables(doc)[0]), expected)
        self.assertEqual(tables(doc)[0].direction, "rtl")


if __name__ == "__main__":
    unittest.main()
