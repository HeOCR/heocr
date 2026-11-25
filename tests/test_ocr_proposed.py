"""D3g on OCR'd pages: learned table proposals become grids read cell by cell, like ruled tables (D3e).

The fake engine reads a cell by its grey level, and a stub stands in for TableFormer. Synthetic fixtures only.
"""

import unittest
from unittest import mock

import heocr
from heocr.pdf.proposed import image_spans
from tests import test_ocr_tables as d3e  # its OcrTableTest is not imported by name: it would run here again
from tests.test_ocr_tables import DPI, FAKE, LEVELS, XS, YS, px, scan, scanned_pdf

SCALE = DPI / 72


def borderless(cells=LEVELS):
    """The D3e test table with no rulings: only the grey 'text' blocks."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (px(595), px(842)), 255)
    draw = ImageDraw.Draw(image)
    for r, row in enumerate(cells):
        for c, grey in enumerate(row):
            draw.rectangle([px(XS[c] + 30), px(YS[r] + 12), px(XS[c] + 100), px(YS[r] + 28)], fill=grey)
    return image


def proposal():
    """Cell boxes hugging the blocks (top-left frame, points), columns left to right."""
    cells = [{"row": r, "col": c, "box": [XS[c] + 30, YS[r] + 12, XS[c] + 100, YS[r] + 28]}
             for r in range(3) for c in range(3)]
    return {"bbox": [XS[0], YS[0], XS[-1], YS[-1]], "rows": 3, "cols": 3, "cells": cells}


class Stub:
    def __init__(self, proposals=None, fail=False):
        self.proposals = proposals if proposals is not None else [proposal()]
        self.fail = fail
        self.record = {"proposer": "stub"}
        self.calls = []

    def page_tables(self, body, index, width, height):
        self.calls.append((body[:5], index, width, height))
        if self.fail:
            raise RuntimeError("stub failure")
        return self.proposals


class OcrProposedTest(unittest.TestCase):
    setUp = d3e.OcrTableTest.setUp

    def read(self, image, stub):
        with mock.patch("heocr.tableformer.TableFormerProposer", lambda models: stub):
            return heocr.extract(scanned_pdf(image), tesseract=FAKE, check_numbers=False, tables="tableformer",
                                 tables_models="unused")

    def test_a_borderless_table_in_a_scan_is_read_cell_by_cell(self) -> None:
        without = heocr.extract(scanned_pdf(borderless()), tesseract=FAKE, check_numbers=False)
        self.assertEqual([b for b in without.blocks if b.kind == "table"], [])
        stub = Stub()
        doc = self.read(borderless(), stub)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in table.cells],
                         [["שם", "תפקיד", "סכום"], ["ספק א", "מנהל", "100"], ["ספק ב", "עובד", "200"]])
        self.assertIn("tableformer_proposed", table.flags)
        self.assertNotIn("ruled_table", table.flags)
        self.assertIn("tableformer_tables", doc.pages[0].flags)
        # Docling saw the page image itself, as a one-page PDF of the page's size in points.
        ((head, index, width, height),) = stub.calls
        self.assertEqual((head, index), (b"%PDF-", 0))
        self.assertAlmostEqual(width, 595, delta=1)
        self.assertAlmostEqual(height, 842, delta=1)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_ruled_grid_keeps_first_claim(self) -> None:
        doc = self.read(scan(LEVELS), Stub())
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertIn("ruled_table", table.flags)
        self.assertNotIn("tableformer_proposed", table.flags)
        self.assertNotIn("tableformer_tables", doc.pages[0].flags)

    def test_a_failing_proposer_leaves_the_page_read(self) -> None:
        doc = self.read(borderless(), Stub(fail=True))
        self.assertIn("table_proposer_failed", doc.flags)
        self.assertIn("table_proposer_failed", [u.reason for u in doc.unread])
        self.assertEqual(doc.pages[0].route, "ocr")
        self.assertEqual([b for b in doc.blocks if b.kind == "table"], [])


class ImageSpansTest(unittest.TestCase):
    def test_rulings_are_not_text_lines(self) -> None:
        image = borderless()
        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        for x in XS:  # vertical rulings only: every pixel row of the table holds some ink
            draw.rectangle([px(x) - 1, px(YS[0]), px(x) + 1, px(YS[-1])], fill=0)
        xs, ys = image_spans(image, SCALE, [XS[0], YS[0], XS[-1], YS[-1]], 160)
        self.assertEqual(len(ys), 3)
        for (top, bottom), y in zip(ys, YS):
            self.assertAlmostEqual(top, y + 12, delta=1)
            self.assertAlmostEqual(bottom, y + 28, delta=1)
        # The blocks' columns are inked; the gaps between them are not.
        for c in range(3):
            self.assertTrue(any(a <= XS[c] + 60 <= b for a, b in xs))
            self.assertFalse(any(a <= XS[c] + 115 <= b for a, b in xs))


if __name__ == "__main__":
    unittest.main()
