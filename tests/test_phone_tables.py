"""The opt-in phone plane recovers ruled regions with source-space boxes."""

from io import BytesIO
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.tesseract import OcrLine, OcrResult
from heocr.pdf.perspective import _projection, _source_quad
from heocr.pdf.phone_tables import grids_from_phone


def photographed_form(*, ruled=True):
    image = Image.new("L", (800, 1000), 95)
    quad = ((165, 95), (635, 108), (787, 921), (13, 899))
    pixels = ImageDraw.Draw(image)
    pixels.polygon(quad, fill=229)
    projection = _projection(quad, 630, 860, 0)

    def draw(box):
        pixels.polygon(_source_quad(projection, box), fill=52)

    for top in (140, 337):
        if ruled:
            for y in (top, top + 39, top + 78):
                draw((74, y, 551, y + 3))
            for x in (74, 233, 392, 548):
                draw((x, top, x + 3, top + 81))
        for y in (top + 12, top + 51):
            for x in (99, 260, 418):
                draw((x, y, x + 42, y + 10))
    return image


class PhoneTablesTest(TestCase):
    def test_two_grids_project_back_to_their_source_positions(self):
        image = photographed_form()
        grids = grids_from_phone(image)
        self.assertEqual([(len(g.grid.rows), len(g.grid.xs) - 1) for g in grids], [(2, 3), (2, 3)])
        self.assertLess(grids[0].source_box(grids[0].grid.bbox, 1)[3],
                        grids[1].source_box(grids[1].grid.bbox, 1)[1])
        for g in grids:
            xs, ys = zip(*g.mask)
            self.assertTrue(0 < min(xs) < max(xs) < image.width)
            self.assertTrue(0 < min(ys) < max(ys) < image.height)
        self.assertEqual(grids_from_phone(photographed_form(ruled=False)), [])

    def extract(self, image, **options):
        source = BytesIO()
        image.save(source, "PNG")
        engine = Mock(psm=3, lang="heb", route=None, version="tesseract 5.5.3")
        engine.recognize.return_value = OcrResult([OcrLine("בדיקה", [10, 12, 34, 25], [91])])
        with patch("heocr.pdf.reader.engine_for", return_value=engine):
            return heocr.extract(source.getvalue(), check_numbers=False, **options)

    def test_profile_opt_in_reads_grids_and_keeps_non_phone_route_exact(self):
        image = photographed_form()
        base = self.extract(image, ocr_capture_type="phone")
        candidate = self.extract(image, ocr_capture_type="phone", ocr_phone_tables=True)
        self.assertNotIn("ocr_phone_tables_rectified", base.pages[0].flags)
        self.assertIn("ocr_phone_tables_rectified", candidate.pages[0].flags)
        self.assertEqual(len([b for b in candidate.blocks if b.kind == "table"]), 2)
        self.assertEqual(heocr.validate(candidate.to_dict()), [])
        scan = self.extract(image, ocr_capture_type="scanner")
        scan_opt_in = self.extract(image, ocr_capture_type="scanner", ocr_phone_tables=True)
        self.assertEqual(scan.to_dict(), scan_opt_in.to_dict())

    def test_rejected_cell_ocr_leaves_the_page_unmasked(self):
        with (patch("heocr.pdf.reader._validated_rectified_tables", return_value=None) as validate,
              patch("heocr.pdf.reader._without") as mask):
            doc = self.extract(photographed_form(), ocr_capture_type="phone", ocr_phone_tables=True)
        self.assertEqual(validate.call_count, 2)
        self.assertFalse(mask.called)
        self.assertNotIn("ocr_phone_tables_rectified", doc.pages[0].flags)
        self.assertTrue(doc.text.strip())
