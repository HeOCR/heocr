"""D3c: unruled tables, running text in columns, short side-by-side rows, and rows that continue a ruled table."""

import unittest

import heocr
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

WORDS = ["מכרז", "הצעה", "ספק", "שירות", "חוזה", "תנאי", "מסמך", "ועדה", "משרד", "מועד"]


def sentence(tag: str, n: int, chars: int = 32) -> str:
    """A line of about ``chars`` characters that starts with a recognisable tag."""
    words = [tag, str(n)]
    k = n
    while len(" ".join(words)) < chars:
        words.append(WORDS[k % len(WORDS)])
        k += 3
    return " ".join(words)[:chars].rstrip()


def extract(lines, rules=()):
    return heocr.extract(make_pdf([PdfPage(lines=list(lines), rules=list(rules))]), ocr=False)


def tables_of(doc):
    return [b for b in doc.blocks if b.kind == "table"]


FULL = 76  # characters of a full-width line (about 456 pt): it crosses every gutter


class UnruledTableTest(unittest.TestCase):
    def test_aligned_short_entries_are_an_unruled_table(self) -> None:
        rows = [["שם", "תפקיד", "טלפון"], ["ספק א", "מנהל", "5550"], ["ספק ב", "עובד", "5551"], ["ספק ג", "יועץ", "5552"]]
        lines = lines_block([sentence("פתיחה", 1, FULL)], top=760)
        for r, row in enumerate(rows):
            for c, text in enumerate(row):  # logical order: the first cell is the rightmost
                lines.append(TextLine(text, 545 - c * 150, 700 - r * 24))
        lines += lines_block([sentence("סיום", 2, FULL)], top=560)
        doc = extract(lines)
        (table,) = tables_of(doc)
        self.assertIn("unruled_table", table.flags)
        self.assertEqual([[c.text for c in row] for row in table.cells], rows)
        page = doc.pages[0]
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        self.assertIn("unruled_tables", page.flags)
        cov = page.coverage
        self.assertEqual(cov["chars_in_tables"] + cov["chars_in_lines"], cov["chars_read"])
        self.assertNotIn("coverage_mismatch", page.flags)
        kinds = [b.kind for b in doc.blocks]
        self.assertEqual(kinds, ["text", "table", "text"])
        self.assertEqual(heocr.validate(doc.to_dict()), [])


class ColumnFlowTest(unittest.TestCase):
    def test_two_columns_of_running_text_read_right_column_first(self) -> None:
        lines = lines_block([sentence("כותרת", 0, FULL)], top=780)
        for n in range(8):
            y = 740 - n * 16
            lines.append(TextLine(sentence("ימין", n), 545, y))
            lines.append(TextLine(sentence("שמאל", n), 285, y))
        lines += lines_block([sentence("סוף", 9, FULL)], top=580)
        doc = extract(lines)
        text = doc.text
        right = [text.index(sentence("ימין", n)) for n in range(8)]
        left = [text.index(sentence("שמאל", n)) for n in range(8)]
        self.assertEqual(right, sorted(right))
        self.assertEqual(left, sorted(left))
        self.assertLess(right[-1], left[0])  # the whole right column comes first
        self.assertLess(text.index(sentence("כותרת", 0, FULL)), right[0])
        self.assertLess(left[-1], text.index(sentence("סוף", 9, FULL)))
        page = doc.pages[0]
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        self.assertIn("reading_order_column_major_unverified", page.flags)
        self.assertFalse(page.coverage["selection_safe"])
        self.assertEqual(tables_of(doc), [])


class SideBySideRowsTest(unittest.TestCase):
    def test_one_signature_row_between_paragraphs_is_read_row_by_row(self) -> None:
        lines = lines_block([sentence("לפני", 1, FULL)], top=700)
        lines += [TextLine("חתימה: ________", 545, 660), TextLine("תאריך: ________", 200, 660)]
        lines += lines_block([sentence("אחרי", 2, FULL)], top=620)
        doc = extract(lines)
        text = doc.text
        self.assertLess(text.index("חתימה"), text.index("תאריך"))
        page = doc.pages[0]
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        self.assertIn("reading_order_row_major_unverified", page.flags)
        self.assertEqual(tables_of(doc), [])

    def test_a_neighbouring_line_inside_one_band_leaves_the_rows_unresolved(self) -> None:
        # The short line under the right piece could continue it, or follow both: not decided.
        lines = lines_block([sentence("לפני", 1, FULL)], top=700)
        lines += [TextLine("פריט ראשון", 545, 660), TextLine("פריט שני", 200, 660)]
        lines += [TextLine("המשך הפריט", 545, 644)]
        lines += lines_block([sentence("אחרי", 2, FULL)], top=600)
        doc = extract(lines)
        self.assertIn("reading_order_unresolved_columns", doc.pages[0].flags)
        self.assertIn("המשך הפריט", doc.text)

    def test_a_heading_above_a_signature_block_does_not_hold_it_back(self) -> None:
        # The most common case on dev: a heading (or a paragraph's last line) just above a row of
        # signature labels and a row of blanks. It is not lined up with any label, and it sits
        # further from the rows than they are from each other: it cannot be a column's line.
        lines = lines_block([sentence("לפני", 1, FULL)], top=720)
        lines += [TextLine("חתימת המציע:", 330, 684)]
        labels = [("תאריך", 545), ("שם החותם", 380), ("חתימה וחותמת", 200)]
        lines += [TextLine(text, x, 660) for text, x in labels]
        lines += [TextLine("ריק", 530, 646), TextLine("ריק", 365, 646)]
        lines += lines_block([sentence("אחרי", 2, FULL)], top=600)
        doc = extract(lines)
        page = doc.pages[0]
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        text = doc.text
        self.assertLess(text.index("חתימת המציע"), text.index("תאריך"))
        self.assertLess(text.index("תאריך"), text.index("שם החותם"))

    def test_a_line_lined_up_with_a_band_but_far_off_does_not_hold_it_back(self) -> None:
        lines = lines_block([sentence("לפני", 1, FULL)], top=720)
        lines += [TextLine("פריט ראשון", 545, 660), TextLine("פריט שני", 200, 660)]
        lines += [TextLine("המשך הפריט", 545, 660 - 26)]  # lined up, but twice as far as a line spacing (still within NEAR)
        lines += lines_block([sentence("אחרי", 2, FULL)], top=600)
        doc = extract(lines)
        self.assertNotIn("reading_order_unresolved_columns", doc.pages[0].flags)

    def test_labels_in_the_margin_keep_their_paragraphs(self) -> None:
        # A hanging-indent list: "1." at the right, its paragraph indented beside it.
        def para(n: int, k: int) -> str:
            return " ".join(["סעיף" + "אבג"[n - 1] + "אבג"[k]] + [WORDS[(n + k + j) % len(WORDS)] for j in range(8)])

        lines = []
        y = 740
        for n in range(1, 4):
            lines.append(TextLine(f"{n}.", 545, y))
            for k in range(3):
                lines.append(TextLine(para(n, k), 490, y))
                y -= 16
            y -= 8
        doc = extract(lines)
        self.assertEqual(tables_of(doc), [])
        text = doc.text
        for n in range(1, 4):
            self.assertLess(text.index(f"{n}."), text.index(para(n, 0)))
            self.assertLess(text.index(para(n, 0)), text.index(para(n, 2)))
            if n < 3:
                self.assertLess(text.index(para(n, 2)), text.index(f"{n + 1}."))
        page = doc.pages[0]
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        self.assertIn("reading_order_row_major_unverified", page.flags)


XS = [100.0, 215.0, 330.0, 445.0, 545.0]


def ruled_rows(top: float, count: int, height: float = 30.0):
    ys = [top - k * height for k in range(count + 1)]
    rules = [(XS[0], y, XS[-1], y) for y in ys] + [(x, ys[0], x, ys[-1]) for x in XS]
    return rules, ys


class RuledTableContinuedTest(unittest.TestCase):
    def test_a_row_without_borders_joins_the_ruled_tables_around_it(self) -> None:
        upper, ys1 = ruled_rows(700, 2)
        lower, ys2 = ruled_rows(612, 2)
        visual = [["סכום", "תאריך", "תפקיד", "שם"]]  # one row per entry, in logical order below
        logical = [["שם", "תפקיד", "תאריך", "סכום"], ["ספק א", "מנהל", "1.1", "100"], ["ספק ב", "עובד", "2.1", "200"], ["ספק ג", "יועץ", "3.1", "300"], ["ספק ד", "רכז", "4.1", "400"]]
        del visual
        baselines = [ys1[0] - 20, ys1[1] - 20, ys1[2] - 16, ys2[0] - 20, ys2[1] - 20]
        lines = lines_block([sentence("לפני", 1, FULL)], top=760)
        for row, y in zip(logical, baselines):
            for c, text in enumerate(row):  # the first cell is the rightmost column
                lines.append(TextLine(text, XS[4 - c] - 8, y))
        lines += lines_block([sentence("אחרי", 2, FULL)], top=520)
        doc = extract(lines, upper + lower)
        (table,) = tables_of(doc)
        self.assertIn("ruled_table", table.flags)
        self.assertIn("table_rows_unruled", table.flags)
        self.assertEqual([[c.text for c in row] for row in table.cells], logical)
        page = doc.pages[0]
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        cov = page.coverage
        self.assertEqual(cov["chars_in_tables"] + cov["chars_in_lines"], cov["chars_read"])
        self.assertEqual(cov["chars_read"], cov["chars_total"])
        self.assertNotIn("coverage_mismatch", page.flags)

    def test_a_continuing_row_may_span_columns(self) -> None:
        upper, ys1 = ruled_rows(700, 2)
        lower, ys2 = ruled_rows(612, 1)
        lines = [TextLine(t, XS[4 - c] - 8, ys1[r] - 20) for r, row in enumerate([["שם", "תפקיד", "תאריך", "סכום"], ["ספק א", "מנהל", "1.1", "100"]]) for c, t in enumerate(row)]
        lines += [TextLine("ספק ב", XS[4] - 8, ys1[2] - 16), TextLine("הערה ארוכה על פני שני טורים", XS[3] - 8, ys1[2] - 16), TextLine("200", XS[1] - 8, ys1[2] - 16)]
        lines += [TextLine(t, XS[4 - c] - 8, ys2[0] - 20) for c, t in enumerate(["ספק ג", "יועץ", "3.1", "300"])]
        doc = extract(lines, upper + lower)
        (table,) = tables_of(doc)
        shape = [[(c.text, c.colspan) for c in row] for row in table.cells]
        self.assertEqual(shape[2], [("ספק ב", 1), ("הערה ארוכה על פני שני טורים", 2), ("200", 1)])
        self.assertTrue(all(sum(c.colspan for c in row) == 4 for row in table.cells))
        self.assertEqual(len(table.cells), 4)


if __name__ == "__main__":
    unittest.main()
