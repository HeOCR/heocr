"""Invented blank forms with long cells and clipped line runs."""

from pathlib import Path
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, OcrWord, Tesseract
from heocr.pdf.ocrforms import needs_form_probe, preserve_form_cell_lines
from tests.synth.pdf import PdfPage, make_pdf
from tests.test_ocr import FAKE


def reading(*words):
    boxes = [w.bbox for w in words]
    box = [f(b[i] for b in boxes) for i, f in enumerate((min, min, max, max))]
    return OcrLine(" ".join(w.text for w in words), box, [w.confidence for w in words], ("1", "1"), list(words))


def word(text, box, confidence=95):
    return OcrWord(text, list(box), confidence)


class FormMaskTest(TestCase):
    def setUp(self):
        self.form = Image.new("L", (150, 300), 255)
        draw = ImageDraw.Draw(self.form)
        draw.rectangle((12, 20, 39, 27), fill=20)
        self.cell = (7, 14, 74, 67)
        self.outside = word("מחוץ", (81, 25, 108, 33))
        self.missing = word("הערות", (31, 25, 57, 33))
        self.native = [reading(self.outside, self.missing)]
        self.normal_line = reading(word("תיאור", (9, 24, 33, 31)))
        self.result = OcrResult([self.normal_line])

    def test_a_blank_field_and_a_tall_text_cell_trigger_a_probe(self):
        def grid(cells):
            return SimpleNamespace(rows=[[{"box": box, "placeholder": False} for box in cells]])
        self.assertTrue(needs_form_probe(self.form, grid([self.cell, (75, 14, 136, 67)]), 1))
        self.assertFalse(needs_form_probe(self.form, grid([(7, 14, 74, 39), (75, 14, 136, 39)]), 1))

    def test_keeps_a_corroborated_piece_inside_its_cell(self):
        engine = SimpleNamespace(recognize=Mock(return_value=OcrResult([reading(self.missing)])))
        result = preserve_form_cell_lines(engine, self.form, self.cell, 1, self.result, self.native, lambda: False)
        self.assertEqual([line.text for line in result.lines], ["הערות", "תיאור"])
        self.assertIs(result.lines[1], self.normal_line)
        self.assertEqual(result.lines[0].bbox, [22, 9, 48, 17])
        engine.recognize.assert_called_once()

    def test_overlaps_and_unconfirmed_reads_preserve_the_existing_cell(self):
        engine = SimpleNamespace(recognize=Mock())
        overlapping = OcrResult([reading(word("קיים", (21, 8, 45, 16)))])
        result = preserve_form_cell_lines(engine, self.form, self.cell, 1, overlapping, self.native, lambda: False)
        self.assertIs(result, overlapping)
        engine.recognize.assert_not_called()
        engine.recognize.side_effect = OcrUnavailable("invented crop failure")
        result = preserve_form_cell_lines(engine, self.form, self.cell, 1, self.result, self.native, lambda: False)
        self.assertIs(result, self.result)
        engine.recognize.side_effect = None
        engine.recognize.return_value = OcrResult([reading(word("פתיח", (31, 25, 57, 33)))])
        result = preserve_form_cell_lines(engine, self.form, self.cell, 1, self.result, self.native, lambda: False)
        self.assertIs(result, self.result)

    def test_scanned_form_keeps_blank_slots_and_restores_inked_cell(self):
        image = Image.new("L", (600, 800), 255)
        draw = ImageDraw.Draw(image)
        for x in (60, 300, 540):
            draw.line((x, 120, x, 680), width=4, fill=0)
        for y in (120, 400, 680):
            draw.line((60, y, 540, y), width=4, fill=0)
        draw.rectangle((335, 193, 379, 203), fill=20)
        pdf = make_pdf([PdfPage(width=144, height=192, rasters=[(0, 0, 144, 192, image)])])
        body = reading(word("הערות", (334, 192, 380, 204)))

        def recognize(engine, source, dpi=300, psm=None):
            if psm is None and source.convert("L").getpixel((348, 196)) < 50:
                return OcrResult([body])
            if psm == 7 and source.width < 100:
                return OcrResult([body])
            return OcrResult([])

        with TemporaryDirectory() as tmp:
            (Path(tmp) / "heb.traineddata").write_bytes(b"invented model")
            with (patch.dict(os.environ, {"FAKE_TESSDATA": tmp}),
                  patch.object(Tesseract, "recognize", recognize)):
                os.environ.pop("TESSDATA_PREFIX", None)
                doc = heocr.extract(pdf, ocr_model="heb", tesseract=FAKE, check_numbers=False)
        table, = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[cell.text for cell in row] for row in table.cells], [["הערות", ""], ["", ""]])
        self.assertEqual(table.ocr["native_lines_preserved"], 1)
        self.assertNotIn("table_cells_ink_unread", table.flags)
        self.assertFalse(any(entry.reason == "table_row_ink_unread" for entry in doc.unread))
        self.assertEqual(heocr.validate(doc.to_dict()), [])
