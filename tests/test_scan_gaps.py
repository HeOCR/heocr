import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.scan_gaps import bridge_scan_stroke_gaps
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, OcrWord, Tesseract
from tests.synth.pdf import PdfPage, make_pdf


def striped_scan(*streaks):
    image = Image.new("RGB", (1200, 1200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for top in (200, 400):
        for x in range(200, 901, 60):
            draw.rectangle((x, top, x + 5, top + 25), fill=(30, 30, 30))
    for top in streaks:
        draw.rectangle((0, top, image.width - 1, top + 2), fill=(250, 251, 252))
    return image


class ScanStrokeGapTest(unittest.TestCase):
    def test_repeated_short_streaks_restore_only_missing_strokes(self):
        source = striped_scan(208, 408)
        bridged, bands = bridge_scan_stroke_gaps(source)
        self.assertEqual(bands, 2)
        self.assertEqual(bridged.size, source.size)
        self.assertEqual(bridged.mode, source.mode)
        self.assertEqual(source.getpixel((202, 209)), (250, 251, 252))
        self.assertEqual(bridged.getpixel((202, 209)), (30, 30, 30))
        self.assertEqual(bridged.getpixel((218, 209)), (250, 251, 252))
        self.assertEqual(bridged.getpixel((202, 315)), (255, 255, 255))

    def test_intact_print_and_single_streak_keep_the_input(self):
        for source in (striped_scan(), striped_scan(208)):
            with self.subTest():
                bridged, bands = bridge_scan_stroke_gaps(source)
                self.assertIs(bridged, source)
                self.assertEqual(bands, 0)

    def test_pdf_ocr_uses_working_raster_with_original_box_scale(self):
        self.check_pdf_ocr(prose=True)

    def test_pdf_ocr_falls_back_when_the_reading_has_no_prose_column(self):
        self.check_pdf_ocr(prose=False)

    def test_pdf_ocr_falls_back_when_the_repaired_read_fails(self):
        self.check_pdf_ocr(prose=False, fail_candidate=True)

    def check_pdf_ocr(self, prose, fail_candidate=False):
        source = striped_scan(208, 408)
        engine = Tesseract([], None, "heb", 3, "tesseract 5.5.3", "fake", {}, 1)
        boxes = [[180, top, 850, top + 24] for top in range(200, 480, 40)]
        text = "דוגמה של שורה ארוכה בתוך פסקה"
        lines = [OcrLine(text, box, [99.0], (1, 1), [OcrWord(word, box, 99.0) for word in text.split()])
                 for box in boxes]
        raster_values = []

        def recognize(_, image, dpi=300, psm=None):
            if image.size != source.size:
                return OcrResult([])
            self.assertEqual(dpi, 300)
            raster_values.append(image.getpixel((202, 209)))
            if fail_candidate and len(raster_values) == 1:
                raise OcrUnavailable("invented first read failure")
            return OcrResult(lines if prose else lines[:1])

        body = make_pdf([PdfPage(images=[(0, 0, 595, 842)])])
        with (
            patch.object(Tesseract, "find", return_value=engine),
            patch.object(Tesseract, "recognize", recognize),
            patch("heocr.pdf.reader._render", return_value=(source, 300 / 72)),
            patch("heocr.pdf.reader.skew_angle", return_value=0.0),
        ):
            doc = heocr.extract(body, ocr_model="heb", check_numbers=False)
        page, = doc.pages
        before = (250, 251, 252)
        self.assertEqual(raster_values, [(30, 30, 30)] if prose else [(30, 30, 30), before])
        self.assertEqual("ocr_scan_stroke_gaps_bridged" in page.flags, prose)
        self.assertEqual(page.signals.get("ocr_scan_dropout_bands"), 2 if prose else None)
        self.assertEqual([round(value, 2) for value in doc.blocks[0].lines[0].bbox], [value * 72 / 300 for value in boxes[0]])
        self.assertEqual(source.getpixel((202, 209)), before)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
