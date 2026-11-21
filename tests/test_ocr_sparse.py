"""Scanned PDF line recovery with invented OCR readings and ink."""

from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, OcrWord, Tesseract
from tests.synth.pdf import PdfPage, make_pdf
from tests.test_ocr import FAKE


def ocr_line(text, top, left=205, right=475):
    words = [OcrWord(word, [left + 65 * i, top, left + 65 * i + 55, top + 16], 84)
             for i, word in enumerate(text.split())]
    return OcrLine(text, [left, top, right, top + 16], [84] * len(words), ("1", "1"), words)


class SparseScanTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tessdata = Path(directory.name)
        (tessdata / "heb.traineddata").write_bytes(b"invented OCR model")
        environment = patch.dict(os.environ, {"FAKE_TESSDATA": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("TESSDATA_PREFIX", None)
        self.native = [ocr_line("המגדל פתוח בבוקר", 205), ocr_line("כל השבילים מסומנים", 275)]
        self.missed = ocr_line("המעבר לחצר פנוי", 240)
        self.image = Image.new("L", (600, 800), 255)
        draw = ImageDraw.Draw(self.image)
        for y in (205, 240, 275):
            for x in range(211, 475, 19):
                draw.rectangle([x, y + 4, x + 8, y + 13], fill=0)
        for y in (70, 610):
            draw.line((40, y, 560, y), width=5, fill=0)

    def read(self, *, raster=False, ruled=True, psm=3, reread=None):
        image = self.image.copy()
        if not ruled:
            draw = ImageDraw.Draw(image)
            for y in (70, 610):
                draw.rectangle([0, y-5, 600, y+5], fill=255)
            draw.rectangle([0, 700, 52, 750], fill=0)
            # This patch has ink but no long ruling.
        if raster:
            buffer = BytesIO()
            image.save(buffer, format="PNG", dpi=(300, 300))
            source = buffer.getvalue()
        else:
            source = make_pdf([PdfPage(width=144, height=192, rasters=[(0, 0, 144, 192, image)])])
        calls = []

        def recognize(engine, image, dpi=300, psm=None):
            calls.append(psm)
            if psm == 11:
                return OcrResult([self.native[0], ocr_line("כניסה דרך שער", 240, left=9, right=125), self.missed])
            if psm == 7:
                if reread is None:
                    return OcrResult([self.missed])
                if isinstance(reread, Exception):
                    raise reread
                return OcrResult([ocr_line(reread, 240)])
            return OcrResult(self.native)

        with patch.object(Tesseract, "recognize", recognize):
            doc = heocr.extract(source, tesseract=FAKE, ocr_model="heb", ocr_psm=psm, check_numbers=False)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        return doc, calls

    def test_adds_a_corroborated_missing_line_in_the_existing_column(self):
        doc, calls = self.read()
        self.assertEqual(calls, [None, 11, 7])
        self.assertEqual([line.text for block in doc.blocks for line in block.lines], [
            self.native[0].text, self.missed.text, self.native[1].text,
        ])
        self.assertIn("ocr_same_column_lines_recovered", doc.pages[0].flags)
        self.assertEqual(doc.pages[0].coverage["ocr_same_column_lines_recovered"], 1)

    def test_disagreement_and_probe_failure_preserve_original_lines(self):
        for rejected in ("המחסן סגור", OcrUnavailable("fixture failure")):
            with self.subTest(rejected=rejected):
                doc, calls = self.read(reread=rejected)
                self.assertEqual(calls, [None, 11, 7])
                self.assertEqual([line.text for block in doc.blocks for line in block.lines], [
                    line.text for line in self.native
                ])
                self.assertNotIn("ocr_same_column_lines_recovered", doc.pages[0].flags)
                self.assertIn("ocr_short_for_ink", doc.pages[0].flags)

    def test_direct_images_explicit_segmentation_and_unruled_scans_skip_the_retry(self):
        for options in ({"raster": True}, {"psm": 6}, {"ruled": False}):
            with self.subTest(options=options):
                doc, calls = self.read(**options)
                self.assertEqual(calls, [None])
                self.assertNotIn("ocr_same_column_lines_recovered", doc.pages[0].flags)
