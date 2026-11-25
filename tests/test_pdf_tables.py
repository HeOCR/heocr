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
        self.assertEqual([t for t in tables_of(doc) if "ruled_table" in t.flags], [])
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


TALL = [700.0, 640.0, 610.0, 580.0]  # a header row tall enough for three lines of text
LEADING = 14.0


def tall_grid():
    return [(XS[c], y, XS[c + 1], y) for y in TALL for c in range(3)] + [(x, TALL[r], x, TALL[r + 1]) for x in XS for r in range(3)]


def multiline_page(shaded: bool, **page) -> PdfPage:
    """A table whose header cells hold one, two and three lines. With shading, as Word draws it:
    a rectangle per cell, and one per line of text inside it, inset from the cell's sides."""
    header = [["לא", "קיים", "רלוונטי"], ["תעודה"], ["סעיף", "במכרז"]]  # visual order, left to right
    body = [["כן", "אישור רישום", "3.1.1"], ["לא", "אישור ניהול", "3.1.2"]]
    lines, fills = [], []
    for c, texts in enumerate(header):
        for k, text in enumerate(texts):
            lines.append(TextLine(text, XS[c + 1] - 8, TALL[0] - 14 - k * LEADING, size=11))
            if shaded:
                top = TALL[0] - 2 - k * LEADING
                fills.append((XS[c] + 5, top - LEADING, XS[c + 1] - XS[c] - 10, LEADING))
        if shaded:
            fills.append((XS[c], TALL[1], XS[c + 1] - XS[c], TALL[0] - TALL[1]))
    for r, row in enumerate(body, start=1):
        for c, text in enumerate(row):
            lines.append(TextLine(text, XS[c + 1] - 8, TALL[r] - 20))
    return PdfPage(lines=lines, rules=tall_grid(), fills=fills, **page)


class RowsAndBordersTest(unittest.TestCase):
    """The lead's D3b review: multi-line cells split into rows, and doubled borders made spans."""

    def test_a_cell_of_several_lines_stays_one_cell(self) -> None:
        for shaded in (False, True):
            with self.subTest(shaded=shaded):
                doc = heocr.extract(make_pdf([multiline_page(shaded)]), ocr=False)
                (table,) = tables_of(doc)
                cells = [[c.text for c in row] for row in table.cells]
                self.assertEqual(
                    cells,
                    [["סעיף\nבמכרז", "תעודה", "לא\nקיים\nרלוונטי"], ["3.1.1", "אישור רישום", "כן"], ["3.1.2", "אישור ניהול", "לא"]],
                )
                self.assertTrue(all(c.colspan == 1 and c.rowspan == 1 for row in table.cells for c in row))
                self.assertNotIn("table_rows_independent", table.flags)
                cov = doc.pages[0].coverage
                self.assertEqual(cov["chars_in_tables"], cov["chars_read"])

    def test_double_and_thick_borders_make_no_spans(self) -> None:
        variants = [
            {"rules_as": "double", "rule_gap": 3.0},
            {"rules_as": "double", "rule_gap": 4.5},
            {"rules_as": "stroke", "rule_width": 3.0},
            {"rules_as": "rects"},
        ]
        for variant in variants:
            with self.subTest(**variant):
                page = table_page(VISUAL)
                for key, value in variant.items():
                    setattr(page, key, value)
                doc = heocr.extract(make_pdf([page]), ocr=False)
                (table,) = tables_of(doc)
                self.assertEqual([[c.text for c in row] for row in table.cells], [["שם", "תפקיד", "סכום"], ["ספק א", "מנהל", "100 ₪"], ["ספק ב", "עובד", "200 ₪"]])
                self.assertTrue(all(c.colspan == 1 for row in table.cells for c in row))

    def test_shading_alone_is_not_a_table(self) -> None:
        # Shaded boxes side by side (a page banner, a coloured background) have no rulings.
        fills = [(XS[c], YS[r + 1], XS[c + 1] - XS[c], YS[r] - YS[r + 1]) for r in range(3) for c in range(3)]
        page = table_page(VISUAL, rules=[])
        page.fills = fills
        doc = heocr.extract(make_pdf([page]), ocr=False)
        # No ruled table; the aligned text itself may still be read as an unruled one (D3c).
        self.assertEqual([t for t in tables_of(doc) if "ruled_table" in t.flags], [])
        self.assertIn("ספק א", doc.text)


BRACKETED = ["סעיף (א) קובע את התנאים", "המציע (להלן: הספק) יגיש", "על פי ההוראות (ראו נספח ב)", "והכול כמפורט (בכפוף לדין)"]


class CellRepairsTest(unittest.TestCase):
    """Text in table cells gets the repairs a paragraph gets, logged on the table block."""

    def page(self, **kwargs):
        rows = [["סכום", "תפקיד", "שם"], ["100 ₪", "\uf0b7 מנהל", "ספק (א)"], ["200 ₪", "עובד", "ספק נוסף"]]
        extra = [TextLine(t, 545, 800 - 14 * i) for i, t in enumerate(BRACKETED)]  # the document's bracket evidence
        page = table_page(rows, extra=extra)
        for key, value in kwargs.items():
            setattr(page, key, value)
        return page

    def test_bullets_brackets_and_eth_in_cells_are_repaired_and_logged(self) -> None:
        doc = heocr.extract(make_pdf([self.page(remap={"נ": "ð"}, font_name="Broken")]), ocr=False)
        (table,) = tables_of(doc)
        cells = [[c.text for c in row] for row in table.cells]
        self.assertEqual(cells[1], ["ספק (א)", "• מנהל", "100 ₪"])
        self.assertEqual(cells[2][0], "ספק נוסף")
        rules = {r["rule"] for r in table.repairs}
        self.assertEqual(rules, {"symbol_font_bullet", "rtl_bracket_order", "latin1_eth_nun"})
        self.assertIn("brackets_mirrored", table.flags)
        eth = [r for r in table.repairs if r["rule"] == "latin1_eth_nun"]
        self.assertEqual(eth[0]["count"], 2)  # מנהל and נוסף, in two cells

    def test_repairs_off_leaves_cells_as_extracted(self) -> None:
        doc = heocr.extract(make_pdf([self.page()]), ocr=False, repairs=False)
        (table,) = tables_of(doc)
        self.assertEqual([c.text for c in table.cells[1]], ["ספק )א(", "\uf0b7 מנהל", "100 ₪"])
        self.assertEqual(table.repairs, [])


if __name__ == "__main__":
    unittest.main()


class OpenSidedTableTest(unittest.TestCase):
    """F10: a grid drawn without its outer verticals, its rulings running out to the table's edges."""

    @staticmethod
    def open_rules(left_end=XS[0], right_end=XS[3]):
        rules = [(left_end, y, right_end, y) for y in YS]
        for x in XS[1:3]:
            rules += [(x, YS[0], x, YS[3])]
        return rules

    def test_open_sides_are_columns(self) -> None:
        doc = heocr.extract(make_pdf([table_page(VISUAL, rules=self.open_rules())]), ocr=False)
        (table,) = tables_of(doc)
        self.assertEqual([[c.text for c in row] for row in table.cells],
                         [["שם", "תפקיד", "סכום"], ["ספק א", "מנהל", "100 ₪"], ["ספק ב", "עובד", "200 ₪"]])
        self.assertIn("table_open_sides_closed", table.flags)

    def test_only_a_side_whose_top_and_bottom_rulings_end_together_opens(self) -> None:
        from heocr.pdf.tables import Rule, _open_sides
        vs = [Rule(233.0, 100.0, 200.0), Rule(366.0, 100.0, 200.0)]
        hs = [Rule(100.0, 100.0, 500.0), Rule(150.0, 100.0, 500.0), Rule(200.0, 160.0, 500.0)]  # bottom stops short left
        self.assertEqual(_open_sides(hs, vs), [Rule(500.0, 100.0, 200.0)])
        hs[-1] = Rule(200.0, 230.0, 370.0)  # neither side runs past the outer verticals: a closed-off grid
        self.assertEqual(_open_sides(hs, vs), [])

    def test_a_running_footer_under_a_table_stays_out_of_it(self) -> None:
        ys = [150.0, 120.0, 90.0, 60.0]  # a ruled table ending in the page's bottom margin band
        rules = [(XS[0], y, XS[3], y) for y in ys] + [(x, ys[0], x, ys[3]) for x in XS]
        lines = lines_block(["פסקה לפני הטבלה"], top=760)
        for r, row in enumerate(VISUAL):
            for c, text in enumerate(row):
                lines.append(TextLine(text, XS[c + 1] - 8, ys[r] - 20))
        lines += [TextLine("קובץ לדוגמה", XS[3] - 8, 45.0), TextLine("17", XS[1] - 8, 45.0)]  # footer, two pieces
        doc = heocr.extract(make_pdf([PdfPage(lines=lines, rules=rules)]), ocr=False)
        (table,) = tables_of(doc)
        self.assertEqual(len(table.cells), 3)
        self.assertIn("קובץ לדוגמה", " ".join(b.text for b in doc.blocks if b.kind != "table"))

