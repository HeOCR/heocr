"""Printed marks use the scan crop and leave the OCR reading intact."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.cli import main
from heocr.ocr.tesseract import OcrResult, Tesseract
from heocr.pdf.form_marks import _repair_mark_reading
from tests.synth.pdf import PdfPage, make_pdf
from tests.test_ocr import FAKE
from tests.test_ocr_forms import reading, word


def bracket_pair(image, x, y, mark, *, square=True):
    """Draw a pair of brackets around a printed mark or empty slot."""
    draw = ImageDraw.Draw(image)
    shape = [(7, 0), (0, 0), (0, 37), (7, 37)] if square else [
        (8, 0), (4, 5), (1, 11), (0, 18), (1, 25), (4, 31), (8, 37),
    ]
    draw.line([(x + a, y + b) for a, b in shape], fill=0, width=3)
    draw.line([(x + 45 - a, y + b) for a, b in shape], fill=0, width=3)
    if mark:
        draw.line((x + 13, y + 5, x + 32, y + 32), fill=0, width=3)
    if mark == "X":
        draw.line((x + 32, y + 5, x + 13, y + 32), fill=0, width=3)
    return x - 1, y - 1, x + 47, y + 39


class FormScanMarksTest(TestCase):
    def test_reads_printed_x_in_square_and_round_brackets(self):
        image = Image.new("L", (500, 300), 255)
        square = bracket_pair(image, 300, 60, "X")
        round_box = bracket_pair(image, 300, 130, "X", square=False)
        blank = bracket_pair(image, 300, 200, None)
        line = reading(word("[א]", square), word("אישור", (220, 60, 290, 97)))
        self.assertEqual(_repair_mark_reading(image, line, line.text), "[X] אישור")
        line = reading(word("()", round_box), word("אישור", (220, 130, 290, 167)))
        self.assertEqual(_repair_mark_reading(image, line, line.text), "(X) אישור")
        line = reading(word("[ ]", blank), word("אישור", (220, 200, 290, 237)))
        self.assertIsNone(_repair_mark_reading(image, line, line.text))

    def test_uses_pixel_evidence_for_a_marked_choice(self):
        image = Image.new("L", (500, 150), 255)
        empty = bracket_pair(image, 325, 40, None)
        marked = bracket_pair(image, 170, 40, "X")
        line = reading(word("אפשרות:", (380, 40, 490, 77)),
                       word("[", (351, empty[1], empty[2], empty[3])),
                       word("]", (empty[0], empty[1], 336, empty[3])),
                       word("כחול", (240, 40, 311, 77)), word("[א]", marked),
                       word("אדום", (85, 40, 155, 77)))
        self.assertEqual(_repair_mark_reading(image, line, line.text), "אפשרות: [ ] כחול [X] אדום")

    def test_abstains_on_one_stroke_unframed_or_clipped_ink(self):
        image = Image.new("L", (500, 200), 255)
        slash = bracket_pair(image, 300, 50, "slash")
        draw = ImageDraw.Draw(image)
        draw.line((15, 105, 35, 132), fill=0, width=3)
        draw.line((35, 105, 15, 132), fill=0, width=3)
        for box in [slash, (5, 100, 45, 140), (-3, 100, 37, 140)]:
            with self.subTest(box=box):
                line = reading(word("[א]", box), word("מסמכים", (30, box[1], 96, box[3])))
                self.assertIsNone(_repair_mark_reading(image, line, line.text))

    def test_full_scan_path_uses_evidence_only_in_opt_in_form_output(self):
        image = Image.new("L", (600, 800), 255)
        marked = bracket_pair(image, 475, 240, "X", square=False)
        reading_title = reading(word("טופס", (340, 130, 390, 155)), word("ניסוי", (280, 130, 330, 155)))
        reading_mark = reading(word("()", marked), word("אישור", (360, 250, 456, 275)))
        draw = ImageDraw.Draw(image)
        for line in (reading_title, reading_mark):
            for token in line.words:
                if token.text != "()":
                    draw.rectangle(token.bbox, fill=0)
        pdf = make_pdf([PdfPage(width=144, height=192, rasters=[(0, 0, 144, 192, image)])])

        def recognize(engine, source, dpi=300, psm=None):
            return OcrResult([reading_title, reading_mark]) if source.size == image.size else OcrResult([])

        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "heb.traineddata").write_bytes(b"invented model")
            src, dst = root / "scan.pdf", root / "output.json"
            src.write_bytes(pdf)
            with (patch.dict(os.environ, {"FAKE_TESSDATA": temp}),
                  patch.object(Tesseract, "recognize", recognize)):
                os.environ.pop("TESSDATA_PREFIX", None)
                kwargs = dict(ocr_model="heb", tesseract=FAKE, check_numbers=False, form_fields=True)
                base = heocr.extract(pdf, **kwargs)
                improved = heocr.extract(pdf, form_scan_marks=True, **kwargs)
                def cli_extract(path, **settings):
                    return heocr.extract(path, **{**settings, "tesseract": FAKE})

                with patch("heocr.cli.extract", side_effect=cli_extract):
                    self.assertEqual(main([str(src), "--ocr-model", "heb", "--check-numbers", "off",
                                           "-f", "forms", "--form-scan-marks", "-o", str(dst)]), 0)
            output = json.loads(dst.read_bytes())
        self.assertEqual(improved.to_dict(), base.to_dict())
        self.assertEqual(improved.text, base.text)
        base_field, = base.to_forms()[0]["sections"][0]["children"]
        improved_field, = improved.to_forms()[0]["sections"][0]["children"]
        self.assertEqual(base_field["value"], "unchecked")
        self.assertEqual(improved_field["value"], "checked")
        self.assertEqual(output, improved.to_forms())
