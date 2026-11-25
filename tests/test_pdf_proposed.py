"""D3g: learned table proposals become D3b grids; heocr reads the cells. Synthetic fixtures only."""

import unittest
from unittest import mock

import heocr
from heocr.contract import Block, Cell
from heocr.pdf.proposed import _cut, _row_above, merge_proposals, to_table
from heocr.pdf.tabletidy import join_label_lines
from heocr.pdf.lines import Atom
from heocr.pdf.tables import Table, _whole_words
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

    def test_overlapping_detector_columns_cut_where_fewest_characters_cross(self):
        # Two columns whose detector boxes overlap (a wide label in one row): numbers fill 100-140 and 165-200, and
        # the label alone crosses 130-175.
        ink = [(100.0 + 4 * i, 103.0 + 4 * i) for i in range(10)] + [(165.0 + 4 * i, 168.0 + 4 * i) for i in range(9)]
        ink.append((130.0, 175.0))
        cut = _cut((100.0, 160.0), (150.0, 200.0), ink, exact=True)
        self.assertTrue(140.0 < cut < 165.0)
        self.assertEqual(_cut((100.0, 160.0), (150.0, 200.0), ink), 155.0)  # an image profile: the plain middle

    def test_a_column_the_detector_merged_is_recovered(self):
        # One detector column holds two numbers per row, 20 points apart; one row's label spills across the gap.
        def chars(text, x, y):
            return [(x + 4 * i, y, x + 4 * i + 4, y + 10, ch) for i, ch in enumerate(text)]
        ink = []
        for r in range(4):
            y = 105 + 20 * r
            ink += chars("א" * 6, 10, y)  # the label column, 0-40
            ink += chars("12.5", 60, y) + chars("7.5", 96, y) if r != 2 else chars("א" * 14, 60, y)
        spans = [(8, 36), (58, 110)]  # the detector's two columns, left to right
        p = {"bbox": [0, 100, 140, 180], "rows": 4, "cols": 2,
             "cells": [{"row": r, "col": c, "box": [spans[c][0], 104 + 20 * r, spans[c][1], 116 + 20 * r]}
                       for r in range(4) for c in range(2)]}
        t = to_table(p, ink)
        self.assertIn("table_columns_recovered", t.flags)
        self.assertEqual(len(t.xs), 4)
        self.assertTrue(76 < t.xs[2] < 96)

    def test_a_detector_span_over_separate_numbers_is_split_and_a_heading_is_not(self):
        def chars(text, x, y):
            return [(x + 4 * i, y, x + 4 * i + 4, y + 10, ch) for i, ch in enumerate(text)]
        boxes = [[[8, 104, 36, 116], [58, 104, 80, 116], [100, 104, 120, 116]],
                 [[8, 124, 36, 136], [58, 124, 80, 136], [100, 124, 120, 136]]]
        cells = [{"row": r, "col": c, "box": boxes[r][c]} for r in range(2) for c in range(3)]
        cells = [c for c in cells if not (c["row"] == 1 and c["col"] == 2)]
        cells[-1]["colspan"] = 2  # the detector merged row 1's two values
        p = {"bbox": [0, 100, 130, 140], "rows": 2, "cols": 3, "cells": cells}
        ink = chars("אבג", 10, 105) + chars("ראשון", 58, 105) + chars("שני", 100, 105)
        ink += chars("אבג", 10, 125) + chars("1.5", 60, 125) + chars("2.5", 102, 125)
        t = to_table(p, ink)
        self.assertIn("table_spans_split", t.flags)
        self.assertEqual([len(row) for row in t.rows], [3, 3])
        cells[-1]["colspan"], cells[-1]["row"] = 2, 0  # the same span over two words of a heading
        p["cells"] = [c for c in cells if not (c["row"] == 0 and c["col"] == 2)]
        ink = chars("אבג", 10, 105) + chars("כותרת", 58, 105) + chars("ארוכה", 100, 105)
        ink += chars("אבג", 10, 125) + chars("1.5", 60, 125) + chars("2.5", 102, 125)
        self.assertNotIn("table_spans_split", to_table(p, ink).flags)

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

    def test_a_word_wider_than_its_column_stays_whole(self):
        # A diagonal-matrix label: one word printed across the line between its column and the next one.
        word = "א" * 15
        lines = lines_block(["פסקה לפני הטבלה"], top=760) + lines_block(["פסקה אחרי הטבלה"], top=560)
        visual = [["סכום", "תפקיד", "שם"], ["100", None, word], ["200", "עובד", "ספק ב"]]
        for r, row in enumerate(visual):
            for c, text in enumerate(row):
                if text:
                    lines.append(TextLine(text, XS[c + 1] - 8, YS[r] - 20))
        doc = read(PdfPage(lines=lines), Stub())
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in table.cells][1], [word, "", "100"])

    def test_letters_of_a_cut_word_go_to_the_cell_holding_most_of_them(self):
        left, right = [0.0, 0.0, 100.0, 20.0], [100.0, 0.0, 200.0, 20.0]
        word = [Atom("א", 76.0 + 6 * i, 5.0, 82.0 + 6 * i, 15.0, 10.0, i) for i in range(10)]  # 4 letters | 6 letters
        number = [Atom(ch, 60.0 + 6 * i, 5.0, 66.0 + 6 * i, 15.0, 10.0, 20 + i) for i, ch in enumerate("10.5")]
        number = [Atom(a.text, a.x0, a.y0 + 30, a.x1, a.y1 + 30, a.size, a.index) for a in number]
        points = _whole_words(word + number, [left, right])
        self.assertEqual({points[id(a)] for a in word}, {(word[4].cx, word[4].cy)})
        self.assertFalse(any(id(a) in points for a in number))  # digits keep their own places

    def test_a_letter_on_a_column_line_stays_with_its_word(self):
        left, right = [0.0, 0.0, 100.0, 20.0], [100.0, 0.0, 200.0, 20.0]
        word = [Atom("א", 73.0 + 6 * i, 5.0, 79.0 + 6 * i, 15.0, 10.0, i) for i in range(5)]  # the last centred on x=100
        points = _whole_words(word, [left, right])
        self.assertEqual({points[id(a)] for a in word}, {(word[0].cx, word[0].cy)})
        whole = [Atom("א", 10.0 + 6 * i, 5.0, 16.0 + 6 * i, 15.0, 10.0, i) for i in range(5)]
        self.assertEqual(_whole_words(whole, [left, right]), {})  # a word inside one cell is left as it is

    def test_a_short_line_a_row_gap_above_the_rows_is_a_missed_first_row(self):
        bands = [(220.0, 230.0), (235.0, 245.0), (250.0, 260.0), (265.0, 275.0)]
        label = [(300.0, 206.0, 330.0, 215.0, "צפונית")]
        self.assertEqual(_row_above(label, 100.0, 500.0, 10, bands), (206.0, 215.0))
        caption = [(150.0, 206.0, 450.0, 215.0, "טבלת דמי נסיעה")]          # wider than a column: a caption
        self.assertIsNone(_row_above(caption, 100.0, 500.0, 10, bands))
        far = [(300.0, 180.0, 330.0, 189.0, "פסקה")]               # more than a row gap above
        self.assertIsNone(_row_above(far, 100.0, 500.0, 10, bands))
        self.assertIsNone(_row_above(label, 100.0, 500.0, 10, bands[:2]))  # too few rows to know the gap

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

    def test_empty_rows_are_dropped_but_spanned_rows_stay(self):
        from heocr.contract import Cell
        from heocr.pdf.tabletidy import tidy
        b = self.block([[Cell("א"), Cell("1")],
                        [Cell(""), Cell("")],
                        [Cell("ב", rowspan=2), Cell("2")],
                        [Cell("", flags=["merged_above"]), Cell("")],
                        [Cell("ג"), Cell("3")]])
        tidy(b)
        self.assertEqual([[c.text for c in row] for row in b.cells], [["א", "1"], ["ב", "2"], ["", ""], ["ג", "3"]])
        self.assertIn("table_empty_rows_dropped", b.flags)

    def test_a_label_spilling_across_a_column_line_is_rejoined_but_spaced_headers_are_not(self):
        from heocr.contract import Cell
        from heocr.pdf.tabletidy import tidy

        def cell(text, x0, x1, y=100.0):
            return Cell(text, _lines=[[text, [x0, y, x1, y + 10]]])
        # a matrix row: values, then a two-word label printed across the next column line (3 points apart)
        spilled = [cell("12.5", 300, 320), cell("כפר", 262, 280), cell("ירוק", 235, 259), Cell(""), Cell("")]
        # a header row: the last two headers sit a column gap apart (20 points)
        header = [cell("שם", 300, 320, 80), cell("סכום", 250, 280, 80), cell("הערות", 200, 230, 80), Cell(""), Cell("")]
        b = self.block([header, spilled])
        tidy(b)
        self.assertEqual([c.text for c in b.cells[1]], ["12.5", "כפר ירוק", ""])
        self.assertEqual([c.text for c in b.cells[0]], ["שם", "סכום", "הערות"])  # the all-empty columns are dropped too
        self.assertIn("table_labels_rejoined", b.flags)

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
    def test_shared_session_loads_one_converter_and_keeps_document_proposals_isolated(self):
        import sys
        from types import ModuleType, SimpleNamespace

        from heocr.tableformer import TableFormerSession

        class Stream(SimpleNamespace):
            pass

        class NativeTable:
            def __init__(self, info):
                x0, y0, x1, y1 = info["bbox"]
                bbox = SimpleNamespace(l=x0, t=y0, r=x1, b=y1)
                bbox.to_top_left_origin = lambda height: bbox
                self.prov = [SimpleNamespace(page_no=1, bbox=bbox)]
                cells = []
                for c in info["cells"]:
                    l, t, r, b = c["box"]
                    cell_box = SimpleNamespace(l=l, t=t, r=r, b=b)
                    cell_box.to_top_left_origin = lambda height, box=cell_box: box
                    cells.append(SimpleNamespace(bbox=cell_box, start_row_offset_idx=c["row"],
                                                 end_row_offset_idx=c["row"] + 1, start_col_offset_idx=c["col"],
                                                 end_col_offset_idx=c["col"] + 1))
                self.data = SimpleNamespace(num_rows=info["rows"], num_cols=info["cols"], table_cells=cells)

        tabular = make_pdf([borderless_page()])
        paragraph = make_pdf([PdfPage(lines=[TextLine("מסמך בדיקה", 520, 700)])])
        page = SimpleNamespace(size=SimpleNamespace(width=595, height=HEIGHT))

        def convert(stream):
            tables = [NativeTable(proposal())] if stream.stream.read() == tabular else []
            return SimpleNamespace(document=SimpleNamespace(tables=tables, pages={1: page}))

        fake = mock.Mock()
        fake.convert.side_effect = convert
        modules = {"docling": ModuleType("docling"),
                   "docling.datamodel.base_models": SimpleNamespace(DocumentStream=Stream)}
        with (mock.patch.dict(sys.modules, modules),
              mock.patch("importlib.metadata.version", return_value="fixture"),
              mock.patch("heocr.tableformer.verify_models", return_value={"model": "fixture"}) as verify,
              mock.patch.object(TableFormerSession, "_make_converter", return_value=fake) as load):
            session = TableFormerSession("unused")
            options = dict(ocr=False, tables="tableformer", tables_models=session)
            first = heocr.extract(tabular, **options)
            middle = heocr.extract(paragraph, **options)
            repeat = heocr.extract(tabular, **options)
        verify.assert_called_once()
        load.assert_called_once()
        self.assertEqual(fake.convert.call_count, 3)
        self.assertIn("tableformer_tables", first.pages[0].flags)
        self.assertNotIn("tableformer_tables", middle.pages[0].flags)
        self.assertEqual(first.to_dict(), repeat.to_dict())

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


def _label(text, top):
    return Cell(text, _lines=[[text, [300.0, top, 330.0, top + 10.0]]])


def _value(text, top):
    return Cell(text, _lines=[[text, [100.0, top, 120.0, top + 10.0]]])


class JoinLabelLinesTest(unittest.TestCase):
    def rows(self, label_top):
        return [[_label("גבעה", 370.0), _value("4.0", 370.0), _value("6.0", 370.0)],
                [_label("עמק/", label_top), Cell(""), Cell("")],
                [_label("שדה ירוק", 394.0), _value("11.0", 394.0), _value("12.0", 394.0)],
                [_label("חוף", 408.0), _value("3.0", 408.0), _value("4.0", 408.0)],
                [_label("גליל", 422.0), _value("8.0", 422.0), _value("3.0", 422.0)]]

    def test_a_label_line_tight_above_its_row_joins_it(self):
        block = Block(kind="table", cells=self.rows(385.0), flags=[])
        self.assertEqual(join_label_lines(block), 1)
        self.assertEqual([row[0].text for row in block.cells], ["גבעה", "עמק/\nשדה ירוק", "חוף", "גליל"])
        self.assertEqual(block.cells[1][1].text, "11.0")

    def test_a_section_label_a_row_gap_above_keeps_its_row(self):
        rows = self.rows(376.0)
        rows[1][0] = _label("(1) אזור", 376.0)
        rows[2][0] = _label("שדה ירוק", 394.0)
        block = Block(kind="table", cells=rows, flags=[])
        self.assertEqual(join_label_lines(block), 0)
        self.assertEqual(len(block.cells), 5)
