import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.regions import unrotate_box
from heocr.ocr.scan_gaps import bridge_scan_stroke_gaps
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, OcrWord, Tesseract
from heocr.pdf import reader
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

    def test_shallow_turn_requires_corroborating_prose_and_restores_page_boxes(self):
        source = striped_scan().rotate(-0.8, Image.Resampling.BICUBIC, fillcolor=(255,) * 3)
        repaired = striped_scan()
        scale = 300 / 72
        angle = 0.8
        engine = Tesseract([], None, "heb", 3, "tesseract 5.5.3", "fake", {}, 1)
        text = "דוגמה של שורה ארוכה בתוך פסקה"
        boxes = [[180, top, 850, top + 24] for top in range(200, 480, 40)]
        lines = [OcrLine(text, box, [99.0], (1, 1), [OcrWord(word, box, 99.0) for word in text.split()])
                 for box in boxes]
        body = make_pdf([PdfPage(images=[(0, 0, 595, 842)])])
        for corroborates, native_repair in ((True, False), (False, False), (True, True)):
            with self.subTest(corroborates=corroborates, native_repair=native_repair):
                page_reads = []
                bridge_calls = []

                def recognize(_, image, dpi=300, psm=None):
                    if image.size != source.size:
                        return OcrResult([])
                    if image is repaired or image is source:
                        page_reads.append(image)
                    return OcrResult(lines if image is source or corroborates else lines[:1])

                def bridge(image):
                    bridge_calls.append(image)
                    return (image, 0) if image is source and not native_repair else (repaired, 2)

                with (
                    patch.object(Tesseract, "find", return_value=engine),
                    patch.object(Tesseract, "recognize", recognize),
                    patch("heocr.pdf.reader._render", return_value=(source, scale)),
                    patch("heocr.pdf.reader.skew_angle", return_value=angle),
                    patch("heocr.ocr.scan_gaps.bridge_scan_stroke_gaps", side_effect=bridge),
                ):
                    doc = heocr.extract(body, ocr_model="heb", check_numbers=False)
                page, = doc.pages
                self.assertIs(bridge_calls[0], source)
                self.assertEqual(len(bridge_calls), 1 if native_repair else 2)
                self.assertEqual(page_reads, [repaired] if corroborates else [repaired, source])
                turned = corroborates and not native_repair
                self.assertEqual("deskewed" in page.flags, turned)
                self.assertEqual("ocr_scan_stroke_gaps_bridged" in page.flags, corroborates)
                self.assertEqual(page.signals.get("deskew_degrees"), angle if turned else None)
                box = [value / scale for value in boxes[0]]
                if turned:
                    box = unrotate_box(box, angle, (source.width / (2 * scale), source.height / (2 * scale)))
                for actual, expected in zip(doc.blocks[0].lines[0].bbox, box, strict=True):
                    self.assertAlmostEqual(actual, expected, places=6)

    def test_scan_dropout_repair_is_confined_to_closed_table_reads(self):
        source = striped_scan(208, 408)
        draw = ImageDraw.Draw(source)
        for y in (120, 320, 530):
            draw.line((150, y, 1050, y), fill=(15,) * 3, width=3)
        for x in (150, 585, 1050):
            draw.line((x, 120, x, 530), fill=(15,) * 3, width=3)
        scale = 300 / 72
        grid, = reader.tables_in(source, scale)
        engine = Tesseract([], None, "heb", 3, "tesseract 5.5.3", "fake", {}, 1)
        body = make_pdf([PdfPage(images=[(0, 0, 595, 842)])])
        for inferred in (False, True):
            with self.subTest(inferred=inferred):
                grid.flags[:] = ["table_open_sides_inferred"] if inferred else []
                with (
                    patch.object(Tesseract, "find", return_value=engine),
                    patch.object(Tesseract, "recognize", return_value=OcrResult([])),
                    patch.object(reader, "_render", return_value=(source, scale)),
                    patch.object(reader, "skew_angle", return_value=0.0),
                    patch.object(reader, "tables_in", return_value=[grid]),
                    patch.object(reader, "_ocr_tables", wraps=reader._ocr_tables) as read_tables,
                ):
                    doc = heocr.extract(body, ocr_model="heb", check_numbers=False)
                page, = doc.pages
                table_image = read_tables.call_args.args[3]
                original = (250, 251, 252)
                self.assertEqual(source.getpixel((202, 209)), original)
                self.assertEqual(table_image.getpixel((202, 209)), original if inferred else (30,) * 3)
                self.assertEqual("ocr_scan_table_stroke_gaps_bridged" in page.flags, not inferred)
                self.assertEqual(page.signals.get("ocr_scan_table_dropout_bands"), None if inferred else 2)
                table, = (block for block in doc.blocks if block.kind == "table")
                self.assertEqual(table.bbox, grid.bbox)

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
