"""D3e: ruled tables on OCR'd pages, found in the image and read cell by cell.

The fake engine "reads" a cell by its grey level ($FAKE_CELLS), so each cell's answer is known
and a cell that holds ink but cannot be read can be made on purpose.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from tests.synth.pdf import PdfPage, make_pdf

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]
DPI = 150
XS = [100, 233, 366, 500]  # column lines, points from the left
YS = [300, 340, 380, 420]  # row lines, points from the top


def px(v: float) -> int:
    return round(v * DPI / 72)


def scan(cells, *, line_width: int = 3):
    """A white page image with a 3x3 ruled table; ``cells[r][c]`` (c from the left) is a grey level or None."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (px(595), px(842)), 255)
    draw = ImageDraw.Draw(image)
    for y in YS:
        draw.rectangle([px(XS[0]), px(y) - line_width // 2, px(XS[-1]), px(y) + line_width // 2], fill=0)
    for x in XS:
        draw.rectangle([px(x) - line_width // 2, px(YS[0]), px(x) + line_width // 2, px(YS[-1])], fill=0)
    for r, row in enumerate(cells):
        for c, grey in enumerate(row):
            if grey is not None:
                draw.rectangle([px(XS[c] + 30), px(YS[r] + 12), px(XS[c] + 100), px(YS[r] + 28)], fill=grey)
    return image


def scanned_pdf(image) -> bytes:
    return make_pdf([PdfPage(rasters=[(0, 0, 595, 842, image)])])


LEVELS = [[40, 50, 60], [70, 80, 90], [100, 110, 120]]
TEXTS = {40: "סכום", 50: "תפקיד", 60: "שם", 70: "100", 80: "מנהל", 90: "ספק א", 100: "200", 110: "עובד", 120: "ספק ב"}


class OcrTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        self.log = Path(self.tmp.name) / "calls.jsonl"
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in TEXTS.items()}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata), "FAKE_LOG": str(self.log), "FAKE_CELLS": str(cells)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def calls(self, psm: str) -> int:
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return sum(1 for line in lines if "--psm" in (a := json.loads(line)) and a[a.index("--psm") + 1] in psm.split("|"))

    def test_table_in_a_scan_is_read_cell_by_cell(self) -> None:
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        page = doc.pages[0]
        self.assertEqual((page.page_class, page.route), ("T5", "ocr"))
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual(table.source, "ocr")
        self.assertEqual(table.ocr["status"], "unverified")
        self.assertEqual((table.ocr["psm"], table.ocr["cells_psm7"], table.ocr["cells_psm6"]), ("per_cell", 9, 0))
        # RTL: a row's first cell is its rightmost.
        self.assertEqual([[c.text for c in row] for row in table.cells], [["שם", "תפקיד", "סכום"], ["ספק א", "מנהל", "100"], ["ספק ב", "עובד", "200"]])
        self.assertIn("ocr_tables", page.flags)
        self.assertEqual(self.calls("6|7"), 9)  # one call per inked cell
        self.assertEqual(self.calls("3"), 1)  # and the page once, around the table
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        self.assertEqual([u.reason for u in doc.unread], [])
        # The page's own OCR lines sit before the table, by their top edge.
        kinds = [b.kind for b in doc.blocks]
        self.assertEqual(kinds[-1], "table")

    def test_thick_lines_make_one_grid(self) -> None:
        doc = heocr.extract(scanned_pdf(scan(LEVELS, line_width=9)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([len(row) for row in table.cells], [3, 3, 3])
        self.assertTrue(all(c.colspan == 1 for row in table.cells for c in row))

    def test_empty_cells_are_not_sent_to_ocr(self) -> None:
        levels = [[40, None, 60], [None, 80, None], [100, None, 120]]
        doc = heocr.extract(scanned_pdf(scan(levels)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in table.cells], [["שם", "", "סכום"], ["", "מנהל", ""], ["ספק ב", "", "200"]])
        self.assertEqual(self.calls("6|7"), 5)
        self.assertEqual(doc.unread, [])

    def test_a_row_whose_ink_ocr_cannot_read_is_never_silent(self) -> None:
        # Grey 140 is ink the fake engine cannot read. The middle row has nothing else.
        levels = [[40, 50, 60], [140, 140, 140], [100, 140, 120]]
        doc = heocr.extract(scanned_pdf(scan(levels)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertIn("table_cells_ink_unread", table.flags)
        self.assertIn("table_cells_ink_unread", doc.pages[0].flags)
        self.assertEqual([c.flags for c in table.cells[1]], [["ocr_empty_with_ink"]] * 3)
        self.assertEqual(table.cells[2][1].flags, ["ocr_empty_with_ink"])
        rows = [u for u in doc.unread if u.reason == "table_row_ink_unread"]
        self.assertEqual(len(rows), 1)  # only the middle row lost everything
        for got, want in zip(rows[0].bbox, [XS[0], YS[1], XS[-1], YS[2]]):
            self.assertAlmostEqual(got, want, delta=1.5)  # pixel rounding

    def test_a_scan_without_rulings_has_no_table(self) -> None:
        from PIL import Image

        doc = heocr.extract(scanned_pdf(Image.new("L", (px(595), px(842)), 255)), tesseract=FAKE)
        self.assertEqual([b for b in doc.blocks if b.kind == "table"], [])
        self.assertEqual(self.calls("6|7"), 0)
        self.assertNotIn("ocr_tables", doc.pages[0].flags)

    def numbers_setup(self, eng: dict) -> None:
        (Path(os.environ["FAKE_TESSDATA"]) / "eng.traineddata").write_bytes(b"fake eng model")
        path = Path(self.tmp.name) / "cells_eng.json"
        path.write_text(json.dumps({str(k): v for k, v in eng.items()}), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"FAKE_CELLS_ENG": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_numeric_cell_misread_by_heb_takes_the_number_model_reading(self) -> None:
        # Stock heb read a cell's "8,500,000" as "00" (dev); the number model (eng) reads it.
        texts = {**TEXTS, 70: "00"}
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
        self.numbers_setup({70: "8,500,000", 100: "200"})
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        amount = table.cells[1][2]
        self.assertEqual(amount.text, "8,500,000")
        self.assertEqual(amount.flags, ["number_read_by_number_model"])
        # Both readings are kept, with their models; the table stays OCR, unverified.
        self.assertEqual(amount.readings, [{"model": "heb", "text": "00", "confidence": 90.0}, {"model": "eng", "text": "8,500,000", "confidence": 90.0}])
        self.assertEqual((table.source, table.ocr["status"]), ("ocr", "unverified"))
        self.assertEqual(doc.to_dict()["blocks"][-1]["cells"][1][2]["readings"][0]["text"], "00")
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        self.assertIn("numbers_read_by_number_model", table.flags)
        self.assertEqual(table.checks, {"numbers": 1, "agreed": 1, "disagreed": 0, "read_by_number_model": 1})
        self.assertEqual(table.cells[2][2].text, "200")  # the readings agree: unchanged
        self.assertEqual(doc.components["tesseract:number_lang"], "eng")

    def test_a_cell_with_words_is_only_compared(self) -> None:
        texts = {**TEXTS, 70: "סך 100"}
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
        self.numbers_setup({70: "XXX 300", 100: "200"})
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        cell = table.cells[1][2]
        self.assertEqual(cell.text, "סך 100")  # never resolved by a guess
        self.assertEqual(cell.flags, ["number_disagreement"])
        self.assertIn("number_disagreement", table.flags)

    def test_no_second_reading_when_number_checks_are_off(self) -> None:
        self.numbers_setup({70: "999", 100: "999"})
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE, check_numbers=False)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual(table.cells[1][2].text, "100")
        self.assertIsNone(table.checks)


class ImageRulingsTest(unittest.TestCase):
    def test_underlines_and_text_strokes_are_not_rulings(self) -> None:
        from PIL import Image, ImageDraw

        from heocr.pdf.ocrtables import image_rulings

        image = Image.new("L", (px(595), px(842)), 255)
        draw = ImageDraw.Draw(image)
        draw.rectangle([px(100), px(100), px(130), px(102)], fill=0)  # a 30 pt underline: too short
        for k in range(20):  # a block of "letters": short strokes
            draw.rectangle([px(100 + k * 8), px(200), px(104 + k * 8), px(212)], fill=0)
        horizontal, vertical = image_rulings(image, DPI / 72)
        self.assertEqual((horizontal, vertical), ([], []))


if __name__ == "__main__":
    unittest.main()
