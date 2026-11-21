"""Invented residual line and fragment checks."""

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.tesseract import OcrResult, Tesseract
from heocr.pdf.ocrfragments import recover_narrow_lines, suppress_contained_fragments
from tests.synth.pdf import PdfPage, make_pdf
from tests.test_ocr import FAKE
from tests.test_ocr_pixel import line, paint


class ResidualScanLineTest(unittest.TestCase):
    def setUp(self):
        self.gray = Image.new("L", (600, 800), 255)
        self.engine = Mock(psm=3)
        self.body = line("הפרח גדל בגינה", (235, 235, 550, 253))
        self.other = line("הענן זז מזרחה", (235, 300, 550, 318))

    def test_remove_only_the_contained_fragment(self):
        fragment = line("4", (245, 240, 247, 246), confidence=30)
        original = OcrResult([fragment, self.body, self.other])
        raw = line(self.body.text, (33, 33, 348, 51))
        flattened = line(self.body.text, (23, 23, 338, 41))
        self.engine.recognize.side_effect = [OcrResult([raw]), OcrResult([flattened])]
        result = suppress_contained_fragments(self.engine, self.gray, original, 300 / 72)
        self.assertEqual(self.engine.recognize.call_count, 2)
        self.assertEqual(len(result.lines), 2)
        self.assertIs(result.lines[0], self.body)
        self.assertIs(result.lines[1], self.other)

    def test_preserve_fragment_with_disagreeing_crop_or_number_evidence(self):
        fragment = line("8", (245, 240, 247, 246), confidence=30)
        original = OcrResult([fragment, self.body, self.other])
        with patch("heocr.pdf.ocrfragments._confirmed", return_value=self.other):
            self.assertIs(suppress_contained_fragments(self.engine, self.gray, original, 300 / 72), original)
        supported = replace(fragment, number_readings=[{"first": "8", "second": "9"}])
        original = OcrResult([supported, self.body, self.other])
        with patch("heocr.pdf.ocrfragments._confirmed", return_value=self.body) as confirm:
            self.assertIs(suppress_contained_fragments(self.engine, self.gray, original, 300 / 72), original)
        confirm.assert_not_called()

    def test_preserve_uncovered_fragment_between_enclosing_words(self):
        fragment = line("4", (328, 240, 330, 246), confidence=30)
        original = OcrResult([fragment, self.body, self.other])
        with patch("heocr.pdf.ocrfragments._confirmed", return_value=self.body) as confirm:
            self.assertIs(suppress_contained_fragments(self.engine, self.gray, original, 300 / 72), original)
        confirm.assert_not_called()

    def check_narrow(self, text):
        draw = ImageDraw.Draw(self.gray)
        paint(draw, 232)
        paint(draw, 298)
        for i in range(10):
            x = 145 + 7 * i
            draw.rectangle((x, 268, x + 2, 279), fill=10)
            draw.rectangle((x, 275, x + 4, 279), fill=10)
        original = OcrResult([self.body, self.other])
        candidate = line(text, (145, 268, 213, 280))
        with patch("heocr.pdf.ocrfragments._confirmed", return_value=candidate) as confirm:
            result = recover_narrow_lines(self.engine, self.gray, original, 300 / 72, .31)
        self.assertEqual(confirm.call_count, 1)
        return result, original

    def test_narrow_prose_gets_its_own_paragraph(self):
        result, original = self.check_narrow("פרח קטן פורח")
        self.assertEqual(len(result.lines), 3)
        recovered = result.lines[1]
        self.assertIs(result.lines[0], original.lines[0])
        self.assertIs(result.lines[2], original.lines[1])
        self.assertNotEqual(recovered.paragraph, result.lines[0].paragraph)

    def test_narrow_standalone_numerals_are_excluded(self):
        result, original = self.check_narrow("725 804 916")
        self.assertIs(result, original)

    def test_scanned_pdf_reports_removed_fragments(self):
        draw = ImageDraw.Draw(self.gray)
        paint(draw, 232)
        paint(draw, 298)
        fragment = line("4", (245, 240, 247, 246), confidence=30)
        pdf = make_pdf([PdfPage(width=144, height=192, rasters=[(0, 0, 144, 192, self.gray)])])
        original = OcrResult([fragment, self.body, self.other])
        with TemporaryDirectory() as folder:
            (Path(folder) / "heb.traineddata").write_bytes(b"invented model")
            with (patch.dict(os.environ, {"FAKE_TESSDATA": folder}),
                  patch.object(Tesseract, "recognize", return_value=original),
                  patch("heocr.pdf.reader.short_for_ink", return_value=(False, .01)),
                  patch("heocr.pdf.ocrfragments._confirmed", return_value=self.body)):
                os.environ.pop("TESSDATA_PREFIX", None)
                doc = heocr.extract(pdf, tesseract=FAKE, ocr_model="heb", check_numbers=False)
        self.assertIn("ocr_contained_fragments_removed", doc.pages[0].flags)
        self.assertEqual(doc.pages[0].coverage["ocr_contained_fragments_removed"], 1)
        self.assertEqual([self.body.text, self.other.text], doc.text.splitlines())
        self.assertEqual(heocr.validate(doc.to_dict()), [])
