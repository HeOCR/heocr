"""Invented baselines and ink for the photo gap reader."""

from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrWord, Tesseract
from heocr.pdf.ocrlines import recover_photo_lines
from tests.test_ocr import FAKE


RECOVERED = ("השביל נפתח מחדש", "אפשר להמתין בחצר", "הכניסה ליד המגדל")


def fixture(slope=0.024):
    image = Image.new("L", (800, 600), 235)
    draw = ImageDraw.Draw(image)

    def shift(x):
        return round(slope * (x - 430))

    # Ink marks provide known row geometry. The fake OCR supplies invented text.
    for top in (177, 209, 240):
        for x in range(220, 666, 13):
            y = top + shift(x)
            draw.rectangle((x, y, x + 5, y + 14), fill=0)

    lines = []
    for i, top in enumerate((80, 140, 280, 325)):
        words = [
            OcrWord("כיתוב", [x, top + shift(x), x + 48, top + shift(x) + 20], 90)
            for x in range(210, 651, 52)
        ]
        box = [
            words[0].bbox[0],
            min(w.bbox[1] for w in words),
            words[-1].bbox[2],
            max(w.bbox[3] for w in words),
        ]
        lines.append(
            OcrLine("שורה קיימת", box, [90] * len(words), (str(i), "1"), words)
        )
    return image, OcrResult(lines)


class Probe:
    psm = 3

    def __init__(self, native, *, disagree=False):
        self.native = native
        self.disagree = disagree
        self.calls = 0

    def recognize(self, image, dpi, psm=None):
        if psm is None:
            return self.native
        if psm != 7:
            return OcrResult([])
        index = self.calls // 2
        text = RECOVERED[index]
        if self.disagree and self.calls % 2:
            text = "כיתוב אחר לגמרי"
        self.calls += 1
        y = image.height / 2
        words = [
            OcrWord(word, [30 + n * 50, y - 5, 65 + n * 50, y + 5], 91)
            for n, word in enumerate(text.split())
        ]
        return OcrResult(
            [OcrLine(text, [30, y - 5, 165, y + 5], [91] * len(words), words=words)]
        )


class PhotoGapTest(unittest.TestCase):
    def test_sheared_gap_restores_three_rows_without_replacing_native(self):
        image, native = fixture()
        probe = Probe(native)
        out = recover_photo_lines(probe, image, native, 72)
        self.assertEqual([line.text for line in out.lines[2:5]], list(RECOVERED))
        self.assertEqual(probe.calls, 6)
        for actual, original in zip(out.lines[:2] + out.lines[5:], native.lines):
            self.assertIs(actual, original)
        for row, line in zip((177, 209, 240), out.lines[2:5]):
            self.assertAlmostEqual((line.bbox[1] + line.bbox[3]) / 2, row + 7, delta=10)
            self.assertGreater(line.words[0].bbox[0], 150)

    def test_disagreeing_reread_retains_the_original_result(self):
        image, native = fixture()
        probe = Probe(native, disagree=True)
        self.assertIs(recover_photo_lines(probe, image, native, 72), native)
        self.assertEqual(probe.calls, 2)

    def test_a_line_emitted_elsewhere_in_the_page_blocks_insertion(self):
        image, native = fixture()
        already_read = replace(native.lines[0], bbox=[200, 170, 700, 197])
        native.lines.append(already_read)
        self.assertIs(recover_photo_lines(Probe(native), image, native, 72), native)

    def test_horizontal_gap_does_not_trigger_recovery(self):
        image, native = fixture(slope=0)
        probe = Probe(native)
        self.assertIs(recover_photo_lines(probe, image, native, 72), native)
        self.assertEqual(probe.calls, 0)

    def test_image_entry_point_reports_recovery_for_jpeg_only(self):
        image, native = fixture()
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "heb.traineddata").write_bytes(b"invented OCR model")
            with patch.dict(os.environ, {"FAKE_TESSDATA": tmp}):
                os.environ.pop("TESSDATA_PREFIX", None)
                for fmt, added in (("JPEG", 3), ("PNG", 0)):
                    probe = Probe(native)
                    encoded = BytesIO()
                    image.save(encoded, format=fmt, dpi=(72, 72))

                    def recognize(engine, raster, dpi=300, psm=None):
                        return probe.recognize(raster, dpi, psm)

                    with (
                        patch.object(Tesseract, "recognize", recognize),
                        patch("heocr.pdf.reader.skew_angle", return_value=0),
                    ):
                        doc = heocr.extract(
                            encoded.getvalue(),
                            tesseract=FAKE,
                            ocr_model="heb",
                            check_numbers=False,
                        )
                    self.assertEqual(
                        doc.pages[0].coverage.get("ocr_photo_gap_lines_recovered", 0),
                        added,
                    )
                    self.assertEqual(
                        "ocr_photo_gap_lines_recovered" in doc.pages[0].flags,
                        bool(added),
                    )
                    self.assertEqual(heocr.validate(doc.to_dict()), [])
