"""D3g: learned table proposals become D3b grids; heocr reads the cells. Synthetic fixtures only."""

import unittest
from unittest import mock

import heocr
from heocr.pdf.proposed import merge_proposals, to_table
from heocr.pdf.tables import Table
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

HEIGHT = 842.0
XS = [100.0, 233.0, 366.0, 500.0]  # column edges, left to right
YS = [700.0, 670.0, 640.0, 610.0]  # row edges, top to bottom (PDF coordinates)
VISUAL = [["סכום", "תפקיד", "שם"], ["100", "מנהל", "ספק א"], ["200", "עובד", "ספק ב"]]


def borderless_page(rules=()):
    lines = lines_block(["פסקה לפני הטבלה"], top=760) + lines_block(["פסקה אחרי הטבלה"], top=560)
    for r, row in enumerate(VISUAL):
        for c, text in enumerate(row):
            lines.append(TextLine(text, XS[c + 1] - 8, YS[r] - 20))
    return PdfPage(lines=lines, rules=list(rules))


def proposal(shift=0.0):
    """What a detector reports: cell boxes hugging the text (top-left frame), columns left to right."""
    cells = []
    for r in range(3):
        for c in range(3):
            cells.append({"row": r, "col": c, "box": [XS[c] + 40, HEIGHT - YS[r] + 6, XS[c + 1] - 6, HEIGHT - YS[r + 1] - 6]})
    return {"bbox": [XS[0] + shift, HEIGHT - YS[0], XS[3] + shift, HEIGHT - YS[3]], "rows": 3, "cols": 3, "cells": cells}


class Stub:
    def __init__(self, proposals=None, fail=False):
        self.proposals, self.fail = proposals if proposals is not None else [proposal()], fail
        self.record = {"proposer": "stub"}

    def page_tables(self, body, index, width, height):
        if self.fail:
            raise RuntimeError("stub failure")
        return self.proposals if index == 0 else []


def read(page, stub):
    with mock.patch("heocr.tableformer.TableFormerProposer", lambda models: stub):
        return heocr.extract(make_pdf([page]), ocr=False, tables="tableformer", tables_models="unused")


class GridTests(unittest.TestCase):
    def test_midpoint_grid_covers_the_table(self):
        t = to_table(proposal())
        self.assertEqual(len(t.rows), 3)
        self.assertEqual(t.xs[0], XS[0])
        self.assertEqual(t.xs[-1], XS[3])
        for left, right in zip(t.xs, t.xs[1:]):
            self.assertLess(left, right)
        self.assertEqual(t.flags, ["tableformer_proposed"])

    def test_spans_become_placeholders_like_native_grids(self):
        p = proposal()
        p["cells"] = [c for c in p["cells"] if (c["row"], c["col"]) not in ((1, 0), (2, 0))]
        p["cells"].append({"row": 1, "col": 0, "rowspan": 2, "colspan": 1, "box": None})
        t = to_table(p)
        self.assertEqual(t.rows[1][0]["rowspan"], 2)
        self.assertTrue(t.rows[2][0]["placeholder"])

    def test_a_text_line_the_detector_skipped_becomes_its_own_row(self):
        p = proposal()
        y0, y1 = HEIGHT - YS[0], HEIGHT - YS[1]  # row 0 is [y0, y1]; push row 1 down to leave a gap
        for c in p["cells"]:
            if c["row"] >= 1:
                c["box"] = [c["box"][0], c["box"][1] + 20, c["box"][2], c["box"][3] + 20]
        label = (300.0, y1 + 4, 360.0, y1 + 14)  # a label line wholly in the gap
        t = to_table(p, [label])
        self.assertEqual(len(t.rows), 4)
        self.assertIn("table_rows_added", t.flags)
        added = t.rows[1][0]["box"]
        self.assertLessEqual(added[1], label[1])
        self.assertGreaterEqual(added[3], label[3])

    def test_grid_lines_avoid_cutting_text(self):
        p = proposal()
        # a character straddling the plain midpoint between rows 0 and 1 (top-left frame)
        mid = ((HEIGHT - YS[0]) + 30 - 6 + (HEIGHT - YS[1]) + 6) / 2
        ink = [(300.0, mid - 3, 306.0, mid + 3)]
        t = to_table(p, ink)
        cut = t.rows[0][0]["box"][3]
        self.assertTrue(cut <= ink[0][1] or cut >= ink[0][3])

    def test_small_proposals_are_not_tables(self):
        self.assertIsNone(to_table({"bbox": [0, 0, 10, 10], "rows": 1, "cols": 4, "cells": []}))

    def test_native_tables_have_first_claim(self):
        native = Table([XS[0], 100.0, XS[3], 300.0], [[{"box": None}]], [])
        merged = merge_proposals([native], [proposal(), proposal(shift=1000.0)])
        self.assertEqual(len(merged), 2)
        self.assertIs(merged[0], native)


class ReaderTests(unittest.TestCase):
    def test_borderless_table_is_read_with_heocr_text_in_rtl_order(self):
        doc = read(borderless_page(), Stub())
        tables = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual(len(tables), 1)
        cells = [[c.text for c in row] for row in tables[0].cells]
        self.assertEqual(cells[0], ["שם", "תפקיד", "סכום"])
        self.assertEqual(cells[2], ["ספק ב", "עובד", "200"])
        self.assertIn("tableformer_proposed", tables[0].flags)
        self.assertNotIn("ruled_table", tables[0].flags)
        self.assertIn("tableformer_tables", doc.pages[0].flags)
        self.assertNotIn("ספק", " ".join(b.text for b in doc.blocks if b.kind != "table"))
        self.assertEqual(doc.options["tables"], {"proposer": "stub"})

    def test_without_the_option_nothing_changes(self):
        doc = heocr.extract(make_pdf([borderless_page()]), ocr=False)
        self.assertFalse(any(b.kind == "table" and "tableformer_proposed" in b.flags for b in doc.blocks))
        self.assertNotIn("tables", doc.options)

    def test_a_failing_proposer_is_reported_and_reading_continues(self):
        doc = read(borderless_page(), Stub(fail=True))
        self.assertIn("table_proposer_failed", doc.flags)
        self.assertIn("table_proposer_failed", doc.pages[0].flags)
        self.assertFalse(any("tableformer_proposed" in b.flags for b in doc.blocks))
        self.assertIn("ספק", doc.text)  # read by the native route (lines or D3c), never dropped

    def test_options_are_validated(self):
        with self.assertRaises(ValueError):
            heocr.extract(make_pdf([borderless_page()]), ocr=False, tables="other", tables_models="x")
        with self.assertRaises(ValueError):
            heocr.extract(make_pdf([borderless_page()]), ocr=False, tables="tableformer")


class TidyTests(unittest.TestCase):
    def block(self, rows):
        from heocr.contract import Block
        return Block(kind="table", cells=rows, page=0, bbox=[0, 0, 1, 1], flags=["tableformer_proposed"])

    def test_empty_columns_are_dropped_and_spans_shrink(self):
        from heocr.contract import Cell
        from heocr.pdf.tabletidy import tidy
        b = self.block([[Cell("כותרת", colspan=3), Cell("")],
                        [Cell("א"), Cell(""), Cell("1"), Cell("")],
                        [Cell("ב"), Cell(""), Cell("2"), Cell("")]])
        tidy(b)
        self.assertEqual([[c.text for c in row] for row in b.cells], [["כותרת"], ["א", "1"], ["ב", "2"]])
        self.assertEqual(b.cells[0][0].colspan, 2)
        self.assertIn("table_empty_columns_dropped", b.flags)

    def test_ruled_tables_are_never_reshaped(self):
        from heocr.contract import Block, Cell
        from heocr.pdf.tabletidy import tidy
        b = Block(kind="table", cells=[[Cell("א"), Cell("")], [Cell("ב"), Cell("")]], page=0, bbox=[0, 0, 1, 1],
                  flags=["ruled_table"])
        tidy(b)
        self.assertEqual(len(b.cells[0]), 2)

    def test_a_section_label_merged_into_a_row_is_split_off(self):
        from heocr.contract import Cell
        from heocr.pdf.tabletidy import tidy
        first = Cell("(1) קטע - קרקע\nיישוב", _lines=[["(1) קטע - קרקע", [300, 100, 400, 110]], ["יישוב", [350, 116, 400, 126]]])
        value = Cell("46.74", _lines=[["46.74", [200, 116, 230, 126]]])
        b = self.block([[Cell("שם"), Cell("סכום")], [first, value]])
        tidy(b)
        self.assertEqual([[c.text for c in row] for row in b.cells],
                         [["שם", "סכום"], ["(1) קטע - קרקע", ""], ["יישוב", "46.74"]])
        self.assertIn("table_section_rows_split", b.flags)

    def test_margin_enumerators_join_their_labels_and_a_numbering_column_stays(self):
        from heocr.contract import Cell
        from heocr.pdf.tabletidy import tidy
        rows = [[Cell(""), Cell("שם"), Cell("סכום")], [Cell(")1("), Cell("קטע"), Cell("")],
                [Cell(""), Cell("יישוב"), Cell("5")], [Cell(""), Cell("חווה"), Cell("6")]]
        b = self.block(rows)
        tidy(b)
        self.assertEqual([[c.text for c in row] for row in b.cells],
                         [["שם", "סכום"], [")1( קטע", ""], ["יישוב", "5"], ["חווה", "6"]])
        self.assertIn("table_enumerators_joined", b.flags)
        numbered = self.block([[Cell(str(i)), Cell("א"), Cell("1")] for i in range(1, 5)])
        tidy(numbered)
        self.assertEqual(len(numbered.cells[0]), 3)  # a column with a number in every row is a column

    def test_a_wrapped_label_is_not_split(self):
        from heocr.contract import Cell
        from heocr.pdf.tabletidy import tidy
        # the value sits beside the label's first line: one wrapped label, not a section row
        first = Cell("שם ארוך\nמאוד", _lines=[["שם ארוך", [300, 100, 400, 110]], ["מאוד", [350, 116, 400, 126]]])
        value = Cell("12", _lines=[["12", [200, 100, 230, 110]]])
        b = self.block([[first, value]])
        tidy(b)
        self.assertEqual(len(b.cells), 1)
        self.assertNotIn("table_section_rows_split", b.flags)

    def test_a_detector_column_over_blank_margin_is_dropped_in_the_reading(self):
        p = proposal()
        p["cols"] = 4  # an extra column over the blank margin left of the table
        p["bbox"][0] = 40.0
        p["cells"] = [dict(c, col=c["col"] + 1) for c in p["cells"]] + \
            [{"row": r, "col": 0, "box": [45.0, HEIGHT - YS[r] + 6, 95.0, HEIGHT - YS[r + 1] - 6]} for r in range(3)]
        doc = read(borderless_page(), Stub([p]))
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([c.text for c in table.cells[0]], ["שם", "תפקיד", "סכום"])
        self.assertIn("table_empty_columns_dropped", table.flags)


class PinTests(unittest.TestCase):
    def test_missing_or_wrong_weights_are_refused(self):
        import tempfile
        from pathlib import Path

        from heocr.tableformer import PINNED, verify_models
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                verify_models(tmp)
            for rel in PINNED:
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"not the pinned weights")
            with self.assertRaisesRegex(ValueError, "differs from the pinned"):
                verify_models(tmp)


if __name__ == "__main__":
    unittest.main()
