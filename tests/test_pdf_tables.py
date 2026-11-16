import unittest

import heocr
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

XS = [100.0, 233.0, 366.0, 500.0]  # column lines, left to right
YS = [700.0, 670.0, 640.0, 610.0]  # row lines, top to bottom (PDF coordinates)


def grid(skip_vertical=(), skip_horizontal=()):
    """Rulings of a 3x3 grid; skip_* drop pieces as (line index, cell index) to merge cells."""
    rules = []
    for i, y in enumerate(YS):
        for c in range(3):
            if (i, c) not in skip_horizontal:
                rules.append((XS[c], y, XS[c + 1], y))
    for j, x in enumerate(XS):
        for r in range(3):
            if (j, r) not in skip_vertical:
                rules.append((x, YS[r], x, YS[r + 1]))
    return rules


def cell(text, col, row, rtl=True):
    """A line inside a cell; col 0 is the leftmost."""
    baseline = YS[row] - 20
    return TextLine(text, XS[col + 1] - 8, baseline) if rtl else TextLine(text, XS[col] + 8, baseline, rtl=False)


def table_page(rows, rules=None, rules_as="stroke", rtl=True, extra=()):
    lines = lines_block(["פסקה לפני הטבלה"], top=760) + lines_block(["פסקה אחרי הטבלה"], top=560) + list(extra)
    for r, row in enumerate(rows):
        for c, text in enumerate(row):  # row given in visual order, left to right
            if text:
                lines.append(cell(text, c, r, rtl))
    return PdfPage(lines=lines, rules=grid() if rules is None else rules, rules_as=rules_as)


VISUAL = [["סכום", "תפקיד", "שם"], ["100 ₪", "מנהל", "ספק א"], ["200 ₪", "עובד", "ספק ב"]]


def tables_of(doc):
    return [b for b in doc.blocks if b.kind == "table"]


class RuledTableTest(unittest.TestCase):
    def test_rtl_table_cells_in_logical_order(self) -> None:
        for rules_as in ("stroke", "rects"):
            with self.subTest(rules_as=rules_as):
                doc = heocr.extract(make_pdf([table_page(VISUAL, rules_as=rules_as)]), ocr=False)
                (table,) = tables_of(doc)
                cells = [[c.text for c in row] for row in table.cells]
                self.assertEqual(cells, [["שם", "תפקיד", "סכום"], ["ספק א", "מנהל", "100 ₪"], ["ספק ב", "עובד", "200 ₪"]])
                self.assertEqual(table.direction, "rtl")
                self.assertIn("ruled_table", table.flags)
                self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_table_sits_between_the_paragraphs_and_its_text_leaves_the_lines(self) -> None:
        doc = heocr.extract(make_pdf([table_page(VISUAL)]), ocr=False)
        kinds = [(b.kind, b.text if b.kind != "table" else "") for b in doc.blocks]
        self.assertEqual(kinds, [("text", "פסקה לפני הטבלה"), ("table", ""), ("text", "פסקה אחרי הטבלה")])
        page = doc.pages[0]
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        cov = page.coverage
        self.assertEqual(cov["chars_in_tables"] + cov["chars_in_lines"], cov["chars_read"])
        self.assertEqual(cov["chars_read"], cov["chars_total"])
        self.assertNotIn("coverage_mismatch", page.flags)

    def test_english_table_in_a_hebrew_document_is_rtl(self) -> None:
        english = [["Amount", "Role", "Name"], ["100", "Manager", "Alpha"], ["200", "Clerk", "Beta"]]
        doc = heocr.extract(make_pdf([table_page(english, rtl=False)]), ocr=False)
        (table,) = tables_of(doc)
        self.assertEqual(table.direction, "rtl")
        self.assertEqual([c.text for c in table.cells[0]], ["Name", "Role", "Amount"])

    def test_spans_are_padded_so_cells_keep_their_columns(self) -> None:
        # Row 1: the two left columns merged (no vertical piece at XS[1] in row 1).
        # Column 2 (rightmost): rows 1 and 2 merged (no horizontal piece at YS[2] in column 2).
        rules = grid(skip_vertical={(1, 1)}, skip_horizontal={(2, 2)})
        rows = [["סכום", "תפקיד", "שם"], ["הערה רחבה", "", "ספק א"], ["200 ₪", "עובד", ""]]
        doc = heocr.extract(make_pdf([table_page(rows, rules=rules)]), ocr=False)
        (table,) = tables_of(doc)
        shape = [[(c.text, c.colspan, c.rowspan, "merged_above" in c.flags) for c in row] for row in table.cells]
        self.assertEqual(
            shape,
            [
                [("שם", 1, 1, False), ("תפקיד", 1, 1, False), ("סכום", 1, 1, False)],
                [("ספק א", 1, 2, False), ("הערה רחבה", 2, 1, False)],
                [("", 1, 1, True), ("עובד", 1, 1, False), ("200 ₪", 1, 1, False)],
            ],
        )
        self.assertTrue(all(sum(c.colspan for c in row) == 3 for row in table.cells))
        self.assertIn("| שם | תפקיד | סכום |", doc.markdown)

    def test_rows_with_their_own_columns_are_read_row_by_row(self) -> None:
        # An L-shaped merge has no global grid; read row by row, nothing is lost.
        rules = grid(skip_vertical={(1, 1)}, skip_horizontal={(2, 0)})
        doc = heocr.extract(make_pdf([table_page(VISUAL, rules=rules)]), ocr=False)
        (table,) = tables_of(doc)
        self.assertIn("table_rows_independent", table.flags)
        self.assertEqual([c.text for c in table.cells[0]], ["שם", "תפקיד", "סכום"])
        cov = doc.pages[0].coverage
        self.assertEqual(cov["chars_read"], cov["chars_total"])

    def test_unresolved_grid_keeps_its_text_as_lines(self) -> None:
        # A band without its outer border cannot be read even row by row: flagged, text kept as lines.
        rules = grid(skip_vertical={(1, 1), (0, 1), (0, 2)}, skip_horizontal={(2, 0)})
        doc = heocr.extract(make_pdf([table_page(VISUAL, rules=rules)]), ocr=False)
        self.assertEqual(tables_of(doc), [])
        self.assertIn("table_grid_unresolved", doc.pages[0].flags)
        self.assertIn("ספק א", doc.text)
        self.assertEqual(doc.pages[0].coverage["chars_read"], doc.pages[0].coverage["chars_total"])

    def test_single_column_boxes_are_not_tables(self) -> None:
        boxes = [(100, y, 500, y) for y in YS] + [(x, YS[0], x, YS[-1]) for x in (100, 500)]
        lines = [TextLine(t, 492, YS[r] - 20) for r, t in enumerate(["שורה ראשונה", "שורה שנייה", "שורה שלישית"])]
        doc = heocr.extract(make_pdf([PdfPage(lines=lines, rules=boxes)]), ocr=False)
        self.assertEqual(tables_of(doc), [])

    def test_a_ruled_frame_around_a_paragraph_is_not_a_table(self) -> None:
        frame = [(90, 780, 510, 780), (90, 740, 510, 740), (90, 740, 90, 780), (510, 740, 510, 780)]
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(["פסקה במסגרת"], top=760), rules=frame)]), ocr=False)
        self.assertEqual(tables_of(doc), [])
        self.assertEqual(doc.text, "פסקה במסגרת\n")


if __name__ == "__main__":
    unittest.main()
