"""D3g: a table printed as side-by-side blocks becomes one logical table (0032 convention a). Invented text only."""

import unittest

from heocr.contract import Block, Cell
from heocr.pdf.desnake import desnake


def block(rows):
    return Block(kind="table", cells=[[c if isinstance(c, Cell) else Cell(c) for c in row] for row in rows], flags=[])


def texts(b):
    return [[c.text for c in row] for row in b.cells]


class DesnakeTest(unittest.TestCase):
    def test_two_blocks_become_one_table_start_block_first(self):
        b = block([["קוד", "תעריף", "קוד", "תעריף"],
                   ["1", "5.00", "4", "6.50"],
                   ["2", "5.50", "5", "7.00"],
                   ["3", "6.00", "", ""]])          # the last block is one row short
        self.assertTrue(desnake(b))
        self.assertEqual(texts(b), [["קוד", "תעריף"], ["1", "5.00"], ["2", "5.50"], ["3", "6.00"],
                                    ["4", "6.50"], ["5", "7.00"]])
        self.assertIn("table_desnaked", b.flags)
        self.assertIn("table_blocks_2", b.flags)

    def test_three_blocks_and_a_two_row_header(self):
        head = ["סעיף", "סכום"] * 3
        sub = ["", "ש\"ח"] * 3
        b = block([head, sub, ["א", "1", "ג", "3", "ה", "5"], ["ב", "2", "ד", "4", "ו", "6"]])
        self.assertTrue(desnake(b))
        self.assertEqual([row[0].text for row in b.cells], ["סעיף", "", "א", "ב", "ג", "ד", "ה", "ו"])
        self.assertIn("table_blocks_3", b.flags)

    def test_a_table_whose_header_does_not_repeat_is_left_alone(self):
        rows = [["שם", "סכום", "שנה", "הערות"], ["א", "1", "2020", ""], ["ב", "2", "2021", ""]]
        b = block(rows)
        self.assertFalse(desnake(b))
        self.assertEqual(texts(b), rows)
        self.assertNotIn("table_desnaked", b.flags)

    def test_a_cell_spanning_a_block_boundary_stops_it(self):
        b = block([["קוד", "תעריף", "קוד", "תעריף"],
                   ["1", Cell("הערה ארוכה", colspan=2), "6.50"]])
        self.assertFalse(desnake(b))

    def test_a_block_without_body_rows_stops_it(self):
        b = block([["קוד", "תעריף", "קוד", "תעריף"], ["1", "5.00", "", ""]])
        self.assertFalse(desnake(b))


if __name__ == "__main__":
    unittest.main()
