"""Perspective-corrected phone OCR on synthetic geometry, with local OCR stubbed."""

from io import BytesIO
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from heocr import extract
from heocr.cli import _parser
from heocr.ocr.phone_geometry import _sheet, read_lines
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, OcrWord
from heocr.pdf.perspective import _projection, _source_quad


def phone_page():
    raster = Image.new("L", (800, 1000), 95)
    quad = ((165, 95), (635, 108), (787, 921), (13, 899))
    draw = ImageDraw.Draw(raster)
    draw.polygon(quad, fill=229)
    transform = _projection(quad, 630, 860, 0)
    lines = []
    for number, y in enumerate(range(160, 590, 47)):
        words = []
        for x in range(73, 552, 51):
            corners = _source_quad(transform, (x, y, x + 35, y + 15))
            draw.polygon(corners, fill=105)
            xs, ys = zip(*corners)
            words.append(OcrWord("בדיקה", [min(xs), min(ys), max(xs), max(ys)], 73))
        if number != 3:
            bounds = [min(w.bbox[0] for w in words), min(w.bbox[1] for w in words),
                      max(w.bbox[2] for w in words), max(w.bbox[3] for w in words)]
            lines.append(OcrLine("בדיקה", bounds, [73] * len(words), words=words))
    return raster, OcrResult(lines)


class PhoneGeometryTest(unittest.TestCase):
    def test_sheet_detection_rejects_flat_or_incomplete_edges(self):
        raster, _ = phone_page()
        self.assertIsNotNone(_sheet(raster))
        self.assertIsNone(_sheet(raster.crop((180, 90, 620, 900))))
        self.assertIsNone(_sheet(Image.new("L", (800, 1000), 230)))

    def test_line_crops_recover_a_missing_band_and_preserve_numbers(self):
        raster, native = phone_page()
        native.lines[1].text += " 4,553"
        old_box = native.lines[4].bbox

        def mock_batch(images, dpi):
            crops = list(images)
            self.assertEqual((len(crops), dpi), (len(native.lines) + 1, 300))
            for crop in crops:
                self.assertLess(crop.height, raster.height / 8)
                self.assertEqual(crop.getpixel((0, 0)), 255)
            readings = []
            for crop in crops:
                box = [16, 14, crop.width - 16, crop.height - 14]
                readings.append(OcrResult([OcrLine("קריאת בדיקה", box, [93],
                                                    words=[OcrWord("קריאת בדיקה", box, 93)])]))
            return readings

        engine = SimpleNamespace(psm=3, recognize_line_batch=mock_batch)
        result = read_lines(engine, raster, native)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.lines), len(native.lines) + 1)
        self.assertIs(result.lines[1], native.lines[1])
        new_box = result.lines[5].bbox
        self.assertLess(abs(new_box[1] - old_box[1]), 12)
        self.assertLess(abs(new_box[3] - old_box[3]), 12)

    def test_batch_failure_leaves_native_result_available(self):
        raster, native = phone_page()
        batch = Mock(side_effect=OcrUnavailable("local failure"))
        self.assertIsNone(read_lines(SimpleNamespace(psm=3, recognize_line_batch=batch), raster, native))
        batch.assert_called_once()

    def test_normal_image_entry_point_gates_by_flag_and_capture(self):
        raster, native = phone_page()
        payload = BytesIO()
        raster.save(payload, format="PNG")
        engine = Mock(psm=3, lang="heb", route=None, version="tesseract 5.5.3")
        engine.recognize.return_value = native
        with (
            patch("heocr.pdf.reader.engine_for", return_value=engine),
            patch("heocr.ocr.phone_geometry.read_lines", return_value=native) as geometry,
        ):
            for capture in ("scanner", "issuer-render", None):
                with self.subTest(capture=capture):
                    options = dict(ocr_capture_type=capture, check_numbers=False)
                    a = extract(payload.getvalue(), **options)
                    b = extract(payload.getvalue(), **options, ocr_phone_geometry=True)
                    self.assertEqual(a.to_json(), b.to_json())
            geometry.assert_not_called()
            extract(payload.getvalue(), ocr_capture_type="pixel", check_numbers=False)
            geometry.assert_not_called()
            doc = extract(payload.getvalue(), ocr_capture_type="pixel", check_numbers=False,
                          ocr_phone_geometry=True)
            geometry.assert_called_once()
            self.assertIn("ocr_phone_geometry_rectified", doc.pages[0].flags)

    def test_cli_exposes_explicit_opt_in(self):
        parser = _parser()
        self.assertFalse(parser.parse_args(["page.jpg"]).ocr_phone_geometry)
        self.assertTrue(parser.parse_args(["page.jpg", "--ocr-phone-geometry"]).ocr_phone_geometry)
