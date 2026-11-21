"""D3e: ruled tables on OCR'd pages, found in the image and read cell by cell.

The fake engine "reads" a cell by its grey level ($FAKE_CELLS), so each cell's answer is known
and a cell that holds ink but cannot be read can be made on purpose.
"""

import json
import os
import shutil
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
        self.assertEqual(self.calls("3"), 0)  # the rest of the page is bare paper: not read (ocr_rest_blank)
        self.assertIn("ocr_rest_blank", page.flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        self.assertEqual([u.reason for u in doc.unread], [])
        # The page's own OCR lines sit before the table, by their top edge.
        kinds = [b.kind for b in doc.blocks]
        self.assertEqual(kinds[-1], "table")

    def textured(self, image):
        """The scan on grey, slightly mottled paper (levels 230-246), as a real scan's paper is."""
        import random

        from PIL import Image

        rng = random.Random(7)
        paper = Image.new("L", image.size)
        paper.putdata([rng.randint(230, 246) for _ in range(image.size[0] * image.size[1])])
        return Image.composite(image, paper, image.point(lambda v: 255 if v < 255 else 0))

    def test_bare_paper_around_a_table_is_not_read(self) -> None:
        # eval's report: a scanned page with one small table. With the table taken out, the rest was
        # bare paper, and Tesseract read its texture as thousands of characters of symbols.
        doc = heocr.extract(scanned_pdf(self.textured(scan(LEVELS))), tesseract=FAKE)
        page = doc.pages[0]
        self.assertIn("ocr_rest_blank", page.flags)
        self.assertEqual(self.calls("3"), 0)
        self.assertEqual([b.kind for b in doc.blocks], ["table"])
        self.assertEqual(page.coverage["status"], "ocr")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_text_beside_a_table_is_still_read(self) -> None:
        from PIL import ImageDraw

        for grey in (0, 185):  # dark text, and faint text (lighter than INK_LEVEL, over 40 levels under the paper)
            with self.subTest(grey=grey):
                self.log.unlink(missing_ok=True)
                image = scan(LEVELS)
                ImageDraw.Draw(image).rectangle([px(100), px(150), px(500), px(170)], fill=grey)
                doc = heocr.extract(scanned_pdf(self.textured(image)), tesseract=FAKE)
                self.assertNotIn("ocr_rest_blank", doc.pages[0].flags)
                self.assertEqual(self.calls("3"), 1)

    def test_a_short_faint_line_beside_a_table_is_read(self) -> None:
        # eval's set E report: lines 43-65 grey levels under the paper, beside a table at 300 dpi, held
        # fewer such pixels than a share of the whole page, and the rest of the page was skipped as bare.
        import random

        from PIL import Image, ImageDraw

        dpi, paper = 300, 236
        p3 = lambda v: round(v * dpi / 72)
        rng = random.Random(11)
        image = Image.new("L", (p3(595), p3(842)), paper)
        draw = ImageDraw.Draw(image)
        for y in YS:  # the same 3x3 ruled table, at 300 dpi
            draw.rectangle([p3(XS[0]), p3(y) - 2, p3(XS[-1]), p3(y) + 2], fill=0)
        for x in XS:
            draw.rectangle([p3(x) - 2, p3(YS[0]), p3(x) + 2, p3(YS[-1])], fill=0)
        for r, row in enumerate(LEVELS):
            for c, grey in enumerate(row):
                draw.rectangle([p3(XS[c] + 30), p3(YS[r] + 12), p3(XS[c] + 100), p3(YS[r] + 28)], fill=grey)
        # One short line of thin strokes, 45 levels under the paper: about 1,600 pixels, under the
        # page-share floor (0.05% of the page, about 4,350) but hundreds in its tiles.
        x = p3(480)
        for _ in range(40):
            h = rng.choice((20, 24, 28))
            draw.rectangle([x, p3(200), x + 1, p3(200) + h], fill=paper - 45)
            x -= rng.choice((6, 8, 10, 14))
        pdf = make_pdf([PdfPage(rasters=[(0, 0, 595, 842, image)])])
        doc = heocr.extract(pdf, tesseract=FAKE)
        self.assertNotIn("ocr_rest_blank", doc.pages[0].flags)
        self.assertEqual(self.calls("3"), 1)  # the rest of the page is read once, around the table

    def empty_page_ocr(self) -> dict:
        tsv = Path(self.tmp.name) / "empty.tsv"
        tsv.write_text("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n1\t1\t0\t0\t0\t0\t0\t0\t1240\t1754\t-1\t\n", encoding="utf-8")
        return {"FAKE_TSV": str(tsv)}

    def test_a_page_that_is_all_table_is_read_not_unread(self) -> None:
        # eval's report: a page whose text is all in a ruled table came out unread / ocr_found_no_text.
        with mock.patch.dict(os.environ, self.empty_page_ocr()):
            doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        page = doc.pages[0]
        self.assertEqual([b.kind for b in doc.blocks], ["table"])
        self.assertEqual(page.coverage["status"], "ocr")
        self.assertGreater(page.coverage["chars_ocr"], 0)
        self.assertEqual(page.coverage["ink_outside_ocr_share"], 0.0)
        self.assertEqual(doc.unread, [])

    def test_ink_outside_the_table_that_ocr_missed_is_partial(self) -> None:
        image = scan(LEVELS)
        from PIL import ImageDraw

        ImageDraw.Draw(image).rectangle([px(100), px(150), px(500), px(170)], fill=0)  # a line of "text" above the table
        with mock.patch.dict(os.environ, self.empty_page_ocr()):
            doc = heocr.extract(scanned_pdf(image), tesseract=FAKE)
        page = doc.pages[0]
        self.assertEqual(page.coverage["status"], "partial")
        self.assertEqual([u.reason for u in doc.unread], ["ocr_short_for_ink"])
        self.assertNotIn("ocr_found_no_text", [u.reason for u in doc.unread])

    def test_the_table_is_masked_in_the_paper_grey_not_white(self) -> None:
        from PIL import Image

        from heocr.pdf.reader import _without, paper_level

        page = scan(LEVELS).point(lambda v: 238 if v == 255 else v)  # grey paper, as a soft scan
        self.assertEqual(paper_level(page), 238)
        masked = _without(page, DPI / 72, [(XS[0], YS[0], XS[-1], YS[-1])])
        inside = masked.crop((px(XS[0]), px(YS[0]), px(XS[-1]), px(YS[-1])))
        self.assertEqual(inside.getextrema(), (238, 238))  # no white patch for a threshold to split on
        rgb = _without(Image.merge("RGB", [page] * 3), DPI / 72, [(XS[0], YS[0], XS[-1], YS[-1])])
        self.assertEqual(rgb.getpixel((px(300), px(360))), (238, 238, 238))
        for mode in ("RGBA", "P", "LA", "CMYK"):
            with self.subTest(mode=mode):
                rgb_page = Image.merge("RGB", [page] * 3)
                source = rgb_page.convert("P", palette=Image.ADAPTIVE) if mode == "P" else rgb_page.convert(mode)  # an adaptive palette keeps 238
                other = _without(source, DPI / 72, [(XS[0], YS[0], XS[-1], YS[-1])])
                self.assertIn(other.mode, ("L", "RGB"))
                self.assertEqual(other.convert("L").getpixel((px(300), px(360))), 238)

    def test_text_layer_masks_use_the_paper_grey_too(self) -> None:
        from PIL import Image

        from heocr.ocr.regions import mask_layer_text

        crop = Image.new("L", (200, 100), 238)
        masked, count = mask_layer_text(crop, (0.0, 0.0), 1.0, [(10, 10, 60, 30)])
        self.assertEqual(count, 1)
        self.assertEqual(masked.getextrema(), (238, 238))

    def region_page(self, image) -> bytes:
        """A text-layer page (T6) with ``image`` as an image region over the middle of the page."""
        from tests.synth.pdf import TextLine, lines_block

        text = lines_block(["המציע יגיש את הצעתו עד המועד הקבוע", "במסמכי המכרז ולא יאוחר מן המועד"], top=800)
        return make_pdf([PdfPage(lines=text + [TextLine("סוף העמוד", 545, 60)], rasters=[(50, 150, 495, 560, image)])])

    def region_image(self, fills):
        """A 495 x 560 pt region at DPI: a 3x3 ruled table at its top; ``fills(draw, x0, y0, x1, y1, r, c)`` paints each cell."""
        from PIL import Image, ImageDraw

        w, h = px(495), px(560)
        image = Image.new("L", (w, h), 255)
        draw = ImageDraw.Draw(image)
        xs, ys = [px(20), px(180), px(330), px(475)], [px(20), px(70), px(120), px(170)]
        for y in ys:
            draw.rectangle([xs[0], y - 1, xs[-1], y + 1], fill=0)
        for x in xs:
            draw.rectangle([x - 1, ys[0], x + 1, ys[-1]], fill=0)
        for r in range(3):
            for c in range(3):
                fills(draw, xs[c], ys[r], xs[c + 1], ys[r + 1], r, c)
        return image

    def test_a_table_in_an_image_region_is_read_cell_by_cell(self) -> None:
        def text_cells(draw, x0, y0, x1, y1, r, c):
            draw.rectangle([x0 + px(30), y0 + px(15), x0 + px(100), y0 + px(30)], fill=LEVELS[r][2 - c])

        doc = heocr.extract(self.region_page(self.region_image(text_cells)), tesseract=FAKE)
        page = doc.pages[0]
        self.assertEqual(page.page_class, "T6")
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in table.cells], [[TEXTS[g] for g in row] for row in LEVELS])
        self.assertIn("from_image_region", table.flags)
        self.assertEqual(table.source, "ocr")
        # Boxes are in page points: the region's top-left is at (50, 842 - 710).
        self.assertAlmostEqual(table.bbox[0], 50 + 20, delta=2)
        self.assertAlmostEqual(table.bbox[1], 842 - 710 + 20, delta=2)
        self.assertIn("image_region_tables", page.flags)
        self.assertEqual(page.coverage["image_region_tables"], 1)
        self.assertIn("המציע יגיש", doc.text)  # the layer text is read as before

    def test_a_table_in_an_image_region_reads_the_same_on_a_rotated_page(self) -> None:
        # /Rotate turns the shown page; the layer and the region are read in the unrotated frame,
        # so the table is the same table at every rotation (on main: none at 90, transposed at 270).
        from tests.synth.pdf import TextLine, lines_block

        def text_cells(draw, x0, y0, x1, y1, r, c):
            draw.rectangle([x0 + px(30), y0 + px(15), x0 + px(100), y0 + px(30)], fill=LEVELS[r][2 - c])

        image = self.region_image(text_cells)
        expected = [[TEXTS[g] for g in row] for row in LEVELS]
        for rotate in (0, 90, 180, 270):
            with self.subTest(rotate=rotate):
                text = lines_block(["המציע יגיש את הצעתו עד המועד הקבוע", "במסמכי המכרז ולא יאוחר מן המועד"], top=800)
                body = make_pdf([PdfPage(lines=text + [TextLine("סוף העמוד", 545, 60)], rasters=[(50, 150, 495, 560, image)], rotate=rotate)])
                turned = make_pdf([PdfPage(lines=text + [TextLine("סוף העמוד", 545, 60)], rasters=[(50, 150, 495, 560, image)], turn=rotate)])
                for variant in (body, turned):  # sideways on screen (user frame), and upright on screen (displayed frame)
                    doc = heocr.extract(variant, tesseract=FAKE)
                    (table,) = [b for b in doc.blocks if b.kind == "table"]
                    self.assertEqual([[c.text for c in row] for row in table.cells], expected)
                    self.assertAlmostEqual(table.bbox[0], 50 + 20, delta=2)
                doc = heocr.extract(body, tesseract=FAKE)
                (table,) = [b for b in doc.blocks if b.kind == "table"]
                self.assertEqual([[c.text for c in row] for row in table.cells], expected)
                self.assertAlmostEqual(table.bbox[0], 50 + 20, delta=2)

    def test_a_chart_with_gridlines_is_not_a_table(self) -> None:
        def bars(draw, x0, y0, x1, y1, r, c):
            draw.rectangle([x0 + px(10), y0 + px(10), x1 - px(10), y1 - px(5)], fill=85)  # bars: a grey the fake reads as no text

        doc = heocr.extract(self.region_page(self.region_image(bars)), tesseract=FAKE)
        self.assertEqual([b for b in doc.blocks if b.kind == "table"], [])
        self.assertNotIn("table_row_ink_unread", [u.reason for u in doc.unread])  # the rejected grid leaves nothing
        self.assertNotIn("image_region_tables", doc.pages[0].flags)
        self.assertTrue([b for b in doc.blocks if "from_image_region" in b.flags])  # region OCR as before

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

    def test_a_skewed_scan_is_deskewed_and_its_table_found(self) -> None:
        # set C office_scan_heavy: at a degree or more of skew a ruling leaves its pixel row before it
        # is long enough to count, and no table was found. The page is turned straight first.
        from PIL import Image

        skewed = scan(LEVELS).rotate(-2.5, resample=Image.NEAREST, fillcolor=255)
        doc = heocr.extract(scanned_pdf(skewed), tesseract=FAKE)
        page = doc.pages[0]
        self.assertIn("deskewed", page.flags)
        self.assertAlmostEqual(page.signals["deskew_degrees"], 2.5, delta=0.15)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in table.cells], [["שם", "תפקיד", "סכום"], ["ספק א", "מנהל", "100"], ["ספק ב", "עובד", "200"]])
        # Its box is in the page's own coordinates: the skewed table's corners, enclosed.
        self.assertLess(abs(table.bbox[0] - XS[0]), 15)
        self.assertLess(abs(table.bbox[3] - YS[-1]), 15)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_skew_at_the_edge_of_the_search_is_not_trusted(self) -> None:
        from PIL import Image

        from heocr.ocr.regions import SKEW_MAX_DEG, skew_angle

        # Turned beyond the search range: the best angle sits on its edge, which says no peak was found.
        self.assertEqual(skew_angle(scan(LEVELS).rotate(SKEW_MAX_DEG + 2, resample=Image.NEAREST, fillcolor=255)), 0.0)
        self.assertAlmostEqual(skew_angle(scan(LEVELS).rotate(-1.5, resample=Image.NEAREST, fillcolor=255)), 1.5, delta=0.15)

    def test_text_is_turned_only_from_a_degree_and_ruled_tables_from_less(self) -> None:
        # set C office scans: turning running text at half a degree cost rows; ruled table cells need straight rows.
        from PIL import Image, ImageDraw

        text = Image.new("L", (px(595), px(842)), 255)
        draw = ImageDraw.Draw(text)
        for y in range(px(120), px(700), px(18)):
            for x in range(px(80), px(510), px(14)):
                draw.rectangle([x, y, x + px(9), y + px(7)], fill=60)  # words, no rulings
        for angle, turned in ((0.6, False), (1.5, True)):
            with self.subTest(text=angle):
                doc = heocr.extract(scanned_pdf(text.rotate(angle, resample=Image.NEAREST, fillcolor=255)), tesseract=FAKE)
                self.assertEqual("deskewed" in doc.pages[0].flags, turned)
        doc = heocr.extract(scanned_pdf(scan(LEVELS).rotate(0.6, resample=Image.NEAREST, fillcolor=255)), tesseract=FAKE)
        self.assertIn("deskewed", doc.pages[0].flags)

    def test_a_straight_scan_is_not_turned(self) -> None:
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        self.assertNotIn("deskewed", doc.pages[0].flags)

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

    def test_a_numeric_cell_heb_read_as_letters_takes_the_number_model_reading(self) -> None:
        # set C pdf_t5_table: heb read "4,370" as "סז5,.?"; letters, but no Hebrew word. And the
        # cell's own ruling comes back as a "|" token, which is no part of the reading.
        texts = {**TEXTS, 70: "סז5,.?"}
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
        self.numbers_setup({70: "4,370 |", 100: "| 200"})
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        amount = table.cells[1][2]
        self.assertEqual(amount.text, "4,370")
        self.assertEqual(amount.flags, ["number_read_by_number_model"])
        self.assertEqual([r["text"] for r in amount.readings], ["סז5,.?", "4,370"])
        self.assertEqual(table.cells[2][2].text, "200")  # the readings agree once the ruling is dropped
        self.assertEqual(table.cells[2][2].readings, None)

    def test_a_well_formed_date_or_amount_is_taken_at_a_lower_confidence(self) -> None:
        # set C: heb read "14.09.2023" as "₪11.22"; eng read it right, at confidence 33.
        for second, taken in (("14.09.2023", True), ("1,312,200", True), ("1409", False), ("14.09.2023 5", False)):
            with self.subTest(second):
                texts = {**TEXTS, 70: "₪11.22"}
                cells = Path(self.tmp.name) / "cells.json"
                cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
                self.numbers_setup({70: second, 100: "200"})
                with mock.patch.dict(os.environ, {"FAKE_CELLS_CONF": "40.0"}):
                    doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
                (table,) = [b for b in doc.blocks if b.kind == "table"]
                self.assertEqual(table.cells[1][2].text, second if taken else "₪11.22")

    def unconfirmed_case(self, heb: str, eng: str, heb_conf: str, eng_conf: str):
        texts = {**TEXTS, 70: heb}
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
        self.numbers_setup({70: eng, 100: "200"})
        with mock.patch.dict(os.environ, {"FAKE_CELLS_CONF": eng_conf, "FAKE_CELLS_CONF_HEB": heb_conf}):
            doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        return doc, table.cells[1][2]

    def half_case(self, heb: str, eng: str | None, half: str, half_conf: str = "60.0", eng_conf: str = "40.0"):
        """Cell 70 read by heb as ``heb``, by the number model as ``eng`` at full resolution and ``half`` at half."""
        texts = {**TEXTS, 70: heb}
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
        self.numbers_setup({70: eng or "", 100: "200"})
        halves = Path(self.tmp.name) / "cells_half.json"
        halves.write_text(json.dumps({"70": half, "100": "200"}), encoding="utf-8")
        env = {"FAKE_CELLS_ENG_HALF": str(halves), "FAKE_CELLS_CONF": eng_conf, "FAKE_CELLS_CONF_HALF": half_conf, "FAKE_CELLS_CONF_HEB": "60.0"}
        with mock.patch.dict(os.environ, env):
            doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        return doc, table.cells[1][2]

    def test_a_number_cell_takes_its_half_resolution_reading_on_its_own_evidence(self) -> None:
        # set C pdf_t5_table*: heb read a date as a lone letter, and eng at 300 dpi as noise; at half
        # resolution eng reads it whole. A whole date or grouped amount is taken on its form.
        doc, cell = self.half_case("ו", None, "17.12.2025")
        self.assertEqual(cell.text, "17.12.2025")
        self.assertIn("number_read_at_half_resolution", cell.flags)
        self.assertEqual(cell.readings[-1]["text"], "17.12.2025")
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        # A plain number is taken when the full-resolution reading agrees, or when it is confident.
        doc, cell = self.half_case("92", "52", "52")
        self.assertEqual(cell.text, "52")
        doc, cell = self.half_case("92", "32", "52", half_conf="90.0")
        self.assertEqual(cell.text, "52")

    def test_an_unsupported_half_resolution_reading_is_not_taken(self) -> None:
        doc, cell = self.half_case("92", "32", "52", half_conf="60.0")
        self.assertNotEqual(cell.text, "52")
        self.assertNotIn("number_read_at_half_resolution", cell.flags)

    def test_a_text_cell_is_not_read_at_half_resolution(self) -> None:
        doc, cell = self.half_case("סכום כולל", None, "1994", half_conf="95.0")
        self.assertEqual(cell.text, "סכום כולל")
        with mock.patch("heocr.pdf.reader.CELL_HALF_READING", False):
            doc, cell = self.half_case("ו", None, "17.12.2025")
        self.assertEqual(cell.text, "ו")

    def test_a_garbled_cell_is_flagged_on_its_own(self) -> None:
        # set C pdf_t5_table: heb read "4,200" as "ססס,,1"; no number reading resolves it. The cell is
        # flagged and recorded unread whatever the other cells' readings, so its row is never lost silently.
        texts = {**TEXTS, 70: "ססס,,1"}
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertIn("table_cell_garbled", table.cells[1][2].flags)
        (entry,) = [u for u in doc.unread if u.reason == "table_cell_garbled"]
        self.assertEqual((entry.scope, entry.page, len(entry.bbox)), ("block", 0, 4))
        self.assertFalse([c for row in table.cells for c in row if c is not table.cells[1][2] and "table_cell_garbled" in (c.flags or [])])
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_faint_row_is_read_or_flagged(self) -> None:
        # set C pdf_t5_table_faint: a row printed in grey 165-196, lighter than the ink test's level, read
        # as three empty cells with no flag. Its cells are read with their contrast stretched, flagged
        # faint_ink_read; when OCR finds nothing, the row is recorded as ink unread.
        doc = heocr.extract(scanned_pdf(scan([[40, 50, 60], [70, 80, 90], [185, 185, 185]])), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        faint = table.cells[2]
        self.assertTrue(all("faint_ink_read" in (c.flags or []) for c in faint))
        self.assertTrue([u for u in doc.unread if u.reason == "table_row_ink_unread"])

    def test_faint_ink_is_told_from_blank_paper(self) -> None:
        from PIL import Image, ImageDraw

        from heocr.pdf.ocrtables import cell_has_faint_ink, cell_has_ink

        blank = Image.new("L", (200, 60), 245)
        faint = blank.copy()
        ImageDraw.Draw(faint).rectangle([20, 20, 180, 40], fill=185)
        self.assertFalse(cell_has_ink(faint))
        self.assertTrue(cell_has_faint_ink(faint))
        self.assertFalse(cell_has_faint_ink(blank))

    def test_a_half_resolution_reading_that_agrees_confirms_the_cell(self) -> None:
        # set C: of the cells flagged unconfirmed after the half-resolution reading, two in three were read
        # right. When that third reading agrees with the cell's text, the cell is not flagged (every such
        # cell was right); when it differs, the flag stays.
        doc, cell = self.half_case("752.25", "1409", "752.25")
        self.assertEqual(cell.text, "752.25")
        self.assertNotIn("table_cell_unconfirmed", cell.flags)
        self.assertFalse([u for u in doc.unread if u.reason == "table_cell_unconfirmed"])
        doc, cell = self.half_case("752.25", "1409", "52")
        self.assertIn("table_cell_unconfirmed", cell.flags)

    def test_a_cell_with_no_digit_in_a_column_of_numbers_is_flagged(self) -> None:
        # set C pdf_t5_table: heb read "4,370" as "סזכ,./" in a column of amounts; nothing else flagged
        # the page, so the row was lost silently. Such a cell is flagged on its own (a header is not).
        from types import SimpleNamespace

        from heocr.contract import Cell
        from heocr.pdf.reader import _flag_cells_out_of_column

        texts = [["פריט", "סכום"], ["ציוד", "1,200"], ["שירות", "סזכ,./"], ["ייעוץ", "3,450"], ["ליווי", "980"]]
        rows = [[Cell(a), Cell(b)] for a, b in texts]
        grid = SimpleNamespace(rows=[[{"col": 1, "box": [10, 20 * r, 50, 20 * r + 18], "placeholder": False},
                                      {"col": 0, "box": [60, 20 * r, 120, 20 * r + 18], "placeholder": False}] for r in range(len(texts))],
                               bbox=[10, 0, 120, 100])
        doc = SimpleNamespace(_rtl=True, unread=[])
        _flag_cells_out_of_column(doc, 0, grid, rows, (0.0, 0.0))
        flagged = [(r, c) for r, row in enumerate(rows) for c, cell in enumerate(row) if "table_cell_not_a_number" in (cell.flags or [])]
        self.assertEqual(flagged, [(2, 1)])
        (entry,) = doc.unread
        self.assertEqual((entry.reason, entry.scope, entry.bbox), ("table_cell_not_a_number", "block", [60, 40, 120, 58]))  # the cell's own box (row 2, second in reading order)

    def test_a_horizontal_merge_does_not_shift_numeric_column_checks(self) -> None:
        from types import SimpleNamespace

        from heocr.contract import Cell
        from heocr.pdf.reader import _flag_cells_out_of_column

        rows = [
            [Cell("פריט"), Cell("סוג"), Cell("כמות")],
            [Cell("עט לסימון", colspan=2), Cell("13")],
            [Cell("דף"), Cell("נייר"), Cell("24")],
            [Cell("סרגל קטן", colspan=2), Cell("תש")],
            [Cell("מחברת קטנה", colspan=2), Cell("35")],
        ]
        grid_rows = []
        for r, row in enumerate(rows):
            right = 3
            boxes = []
            for cell in row:
                left = right - cell.colspan
                boxes.append({"col": left, "box": [30 * left, 20 * r, 30 * right, 20 * r + 18]})
                right = left
            grid_rows.append(boxes[::-1])
        grid = SimpleNamespace(rows=grid_rows, bbox=[0, 0, 90, 98])
        doc = SimpleNamespace(_rtl=True, unread=[])

        _flag_cells_out_of_column(doc, 0, grid, rows, (0.0, 0.0))

        self.assertNotIn("table_cell_not_a_number", rows[2][1].flags or [])
        self.assertIn("table_cell_not_a_number", rows[3][1].flags)
        (entry,) = doc.unread
        self.assertEqual(entry.bbox, [0, 60, 30, 78])

    def test_a_cell_whose_two_readings_differ_and_neither_is_confident_is_unconfirmed(self) -> None:
        doc, cell = self.unconfirmed_case("752.25", "1409", "60.0", "40.0")
        self.assertEqual(cell.text, "752.25")  # nothing chosen: the first reading stays
        self.assertIn("table_cell_unconfirmed", cell.flags)
        self.assertEqual([(r["model"], r["text"], r["confidence"]) for r in cell.readings], [("heb", "752.25", 60.0), ("eng", "1409", 40.0)])
        (entry,) = [u for u in doc.unread if u.reason == "table_cell_unconfirmed"]
        self.assertEqual((entry.scope, entry.page), ("block", 0))
        self.assertEqual(entry.detail, "row 1, column 2: two readings differ and neither is confident")
        self.assertEqual(len(entry.bbox), 4)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_confident_reading_or_agreeing_readings_are_not_unconfirmed(self) -> None:
        for heb, eng, heb_conf, eng_conf in (("752.25", "1409", "90.0", "40.0"), ("752.25", "752.25", "60.0", "40.0"),
                                             ("12׳", "12'", "60.0", "40.0")):
            with self.subTest(heb=heb, eng=eng, heb_conf=heb_conf):
                doc, cell = self.unconfirmed_case(heb, eng, heb_conf, eng_conf)
                self.assertNotIn("table_cell_unconfirmed", cell.flags)
                self.assertFalse([u for u in doc.unread if u.reason == "table_cell_unconfirmed"])

    def test_a_cell_with_a_hebrew_word_is_not_numeric(self) -> None:
        texts = {**TEXTS, 70: "סכום 5"}
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in texts.items()}), encoding="utf-8")
        self.numbers_setup({70: "5", 100: "200"})
        doc = heocr.extract(scanned_pdf(scan(LEVELS)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual(table.cells[1][2].text, "סכום 5")
        self.assertNotIn("number_read_by_number_model", table.cells[1][2].flags)

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
    def test_long_strokes_crossing_in_a_tiny_area_do_not_make_a_table(self) -> None:
        from PIL import Image, ImageDraw

        from heocr.pdf.ocrtables import tables_in

        image = Image.new("L", (300, 200), 255)
        draw = ImageDraw.Draw(image)
        # A crosshatch: every stroke is long, but only a 20 × 20 pt patch is enclosed.
        for k in (20, 30, 40):
            draw.line((0, k, 60, k), fill=0)
            draw.line((k, 0, k, 60), fill=0)
        for y in (100, 130, 160):
            draw.line((100, y, 200, y), fill=0)
        for x in (100, 150, 200):
            draw.line((x, 100, x, 160), fill=0)

        (table,) = tables_in(image, 1)
        self.assertEqual(table.bbox, [100, 100, 200, 160])

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



def grainy_paper(seed: int = 3):
    """Invented scan paper: grey 236 with fine grain, through JPEG at quality 40, at 150 dpi.

    Rendered at 300 dpi, JPEG's ringing around the grain becomes short bars a few pixels long, and
    Tesseract's global threshold reads them as strokes. This is the paper of the gate's synthetic
    T4 page, made from scratch.
    """
    import io
    import random

    from PIL import Image

    rng = random.Random(seed)
    paper = Image.new("L", (px(595), px(842)))
    paper.putdata([max(0, min(255, round(rng.gauss(236, 3)))) for _ in range(paper.size[0] * paper.size[1])])
    buffer = io.BytesIO()
    paper.save(buffer, "JPEG", quality=40)
    buffer.seek(0)
    return Image.open(buffer).convert("L")


def tesseract_with_heb() -> bool:
    """Whether a real Tesseract with ``heb`` is installed (the same lookup heocr itself makes)."""
    if not shutil.which("tesseract") and not os.environ.get("HEOCR_TESSERACT"):
        return False
    from heocr.ocr.tesseract import OcrUnavailable, Tesseract

    try:
        Tesseract.find(lang="heb")
    except OcrUnavailable:
        return False
    return True


@unittest.skipUnless(tesseract_with_heb(), "needs Tesseract with heb installed")
class GrainyPaperTest(unittest.TestCase):
    """Real Tesseract on invented grainy paper around a small ruled table (the T4 noise report)."""

    def test_bare_grainy_paper_is_noise_to_tesseract(self) -> None:
        # The hazard itself: without the skip, the rest of such a page is read as hundreds of characters.
        from PIL import Image

        from heocr.ocr.tesseract import Tesseract

        rendered = grainy_paper().resize((px(595) * 2, px(842) * 2), Image.BILINEAR)
        result = Tesseract.find().recognize(rendered, dpi=300)
        self.assertGreater(sum(len(line.text) for line in result.lines), 300)

    def test_a_table_on_grainy_paper_adds_no_noise(self) -> None:
        from PIL import Image

        paper, table = grainy_paper(), scan(LEVELS)
        page = Image.composite(table, paper, table.point(lambda v: 255 if v < 255 else 0))
        doc = heocr.extract(scanned_pdf(page))
        outside = "".join(b.text or "" for b in doc.blocks if b.kind != "table")
        self.assertEqual(len(outside), 0, "text read from bare paper outside the table")
        self.assertIn("ocr_rest_blank", doc.pages[0].flags)


if __name__ == "__main__":
    unittest.main()
