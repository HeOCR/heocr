"""Invented scans with missing body text, form shading and non-text ink."""

from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrWord, Tesseract
from heocr.pdf.ocrpixel import recover_pixel_lines
from tests.synth.pdf import PdfPage, make_pdf
from tests.test_ocr import FAKE


def line(text, box, confidence=90):
    words = text.split()
    x0, y0, x1, y1 = box
    step = (x1 - x0) / len(words)
    return OcrLine(text, list(box), [confidence] * len(words), ("1", "1"), [
        OcrWord(word, [x0 + i * step, y0, x0 + (i + .85) * step, y1], confidence)
        for i, word in enumerate(words)
    ])


def paint(draw, top, slant=0):
    for i in range(18):
        x = 238 + 17 * i
        y = top + i * slant
        draw.rectangle((x, y + 4, x + 4, y + 15), fill=10)
        draw.rectangle((x, y + 11, x + 8, y + 15), fill=10)


class PixelLineTest(unittest.TestCase):
    def setUp(self):
        self.native = [line("החצר מוארת בבוקר", (230, 230, 550, 248)),
                       line("השער פתוח בערב", (230, 300, 550, 318))]
        self.original = OcrResult(self.native)
        self.gray = Image.new("L", (600, 800), 255)
        draw = ImageDraw.Draw(self.gray)
        paint(draw, 230)
        paint(draw, 300)
        self.text = "המעבר מסומן היטב"

    def shaded_band(self):
        draw = ImageDraw.Draw(self.gray)
        draw.rectangle((224, 263, 557, 284), fill=108)
        paint(draw, 262)

    def reading(self, image, dpi, psm):
        self.assertEqual((dpi, psm), (300, 7))
        y = image.height // 2
        return OcrResult([line(self.text, (22, y - 4, image.width - 22, y + 4))])

    def test_shaded_line_is_inserted_and_native_lines_are_identical(self):
        self.shaded_band()
        engine = type("Reader", (), {"psm": 3, "recognize": staticmethod(self.reading)})()
        for short, share in ((True, .95), (True, .58), (False, .35)):
            with self.subTest(short=short, share=share):
                result = recover_pixel_lines(engine, self.gray, self.original, 300 / 72, share, short=short)
                self.assertEqual(len(result.lines), 3)
                self.assertIs(result.lines[0], self.native[0])
                self.assertIs(result.lines[2], self.native[1])
                self.assertEqual(result.lines[1].text, self.text)
                self.assertTrue(248 < result.lines[1].bbox[1] < result.lines[1].bbox[3] < 300)

    def test_moderate_unread_ink_requires_shading_in_the_crop_read_box(self):
        draw = ImageDraw.Draw(self.gray)
        draw.rectangle((224, 273, 557, 287), fill=108)
        paint(draw, 262)
        engine = type("Reader", (), {"psm": 3})()
        with patch.object(engine, "recognize", create=True, side_effect=self.reading) as recognize:
            self.assertIs(recover_pixel_lines(engine, self.gray, self.original, 300 / 72, .35, short=False),
                          self.original)
        self.assertEqual(recognize.call_count, 2)

    def test_shaded_pass_obeys_uncovered_ink_floor_and_deadline(self):
        self.shaded_band()
        engine = type("Reader", (), {"psm": 3})()
        with patch.object(engine, "recognize", create=True) as recognize:
            self.assertIs(recover_pixel_lines(engine, self.gray, self.original, 300 / 72, .19, short=False),
                          self.original)
            self.assertIs(recover_pixel_lines(engine, self.gray, self.original, 300 / 72, .35,
                                              short=False, stop=lambda: True), self.original)
        recognize.assert_not_called()

    def test_shaded_band_with_standalone_numbers_does_not_add_a_line(self):
        self.shaded_band()
        self.text = "814 927 560"
        engine = type("Reader", (), {"psm": 3, "recognize": staticmethod(self.reading)})()
        self.assertIs(recover_pixel_lines(engine, self.gray, self.original, 300 / 72, .35, short=False),
                      self.original)

    def test_edges_rules_and_rotated_stamp_do_not_propose_lines(self):
        draw = ImageDraw.Draw(self.gray)
        paint(draw, 22)
        paint(draw, 753)
        draw.rectangle((220, 252, 557, 308), fill=108)
        paint(draw, 255, slant=2)
        draw.line((225, 289, 550, 289), fill=0, width=3)
        draw.line((565, 100, 565, 730), fill=0, width=4)
        engine = type("Reader", (), {"psm": 3})()
        with patch.object(engine, "recognize", create=True) as recognize:
            for short, share in ((True, .95), (False, .35)):
                self.assertIs(recover_pixel_lines(engine, self.gray, self.original, 300 / 72, share, short=short),
                              self.original)
        recognize.assert_not_called()

    def test_nonempty_confident_agreement_is_required(self):
        self.shaded_band()
        y = 12
        first = OcrResult([line(self.text, (23, y, 180, y+8))])
        disagree = OcrResult([line("השביל מסומן היטב", (23, 24, 180, 32))])
        uncertain = OcrResult([line(self.text, (23, 24, 180, 32), confidence=40)])
        engine = type("Reader", (), {"psm": 3})()
        for control in (disagree, uncertain, OcrResult([])):
            with self.subTest(control=control):
                with patch.object(engine, "recognize", create=True, side_effect=[first, control]):
                    self.assertIs(recover_pixel_lines(engine, self.gray, self.original, 300 / 72, .95), self.original)

    def test_ocr_page_exposes_pixel_recovery_only_for_scanned_pdf(self):
        self.shaded_band()
        with TemporaryDirectory() as temp:
            (Path(temp) / "heb.traineddata").write_bytes(b"invented model")
            png = BytesIO()
            self.gray.save(png, format="PNG", dpi=(300, 300))
            pdf = make_pdf([PdfPage(width=144, height=192, rasters=[(0, 0, 144, 192, self.gray)])])

            def recognize(engine, image, dpi=300, psm=None):
                if psm == 7:
                    return self.reading(image, dpi, psm)
                return OcrResult(self.native) if psm is None else OcrResult([])

            with (patch.dict(os.environ, {"FAKE_TESSDATA": temp}),
                  patch.object(Tesseract, "recognize", recognize),
                  patch("heocr.pdf.reader.short_for_ink", return_value=(False, .35))):
                os.environ.pop("TESSDATA_PREFIX", None)
                options = {"tesseract": FAKE, "ocr_model": "heb", "check_numbers": False}
                scan = heocr.extract(pdf, **options)
                direct = heocr.extract(png.getvalue(), **options)
            self.assertIn("ocr_pixel_lines_recovered", scan.pages[0].flags)
            self.assertEqual(scan.pages[0].coverage["ocr_pixel_lines_recovered"], 1)
            self.assertNotIn("ocr_pixel_lines_recovered", direct.pages[0].flags)
            self.assertEqual(heocr.validate(scan.to_dict()), [])
