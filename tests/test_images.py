"""D4b: image files read page by page through the OCR route."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from tests.test_ocr_tables import LEVELS, TEXTS, scan

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]


def page_image(ink: bool = True, size=(1240, 1754)):
    """A white A4 page at 150 dpi, with a grey block of "text" unless ``ink`` is False."""
    from PIL import Image, ImageDraw

    image = Image.new("L", size, 255)
    if ink:
        ImageDraw.Draw(image).rectangle([200, 200, 900, 260], fill=40)
    return image


def encoded(image, fmt: str, **kwargs) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **kwargs)
    return out.getvalue()


def glyph_page():
    from PIL import Image, ImageDraw

    image = Image.new("L", (800, 1000), 240)
    draw = ImageDraw.Draw(image)
    for y in range(200, 701, 100):
        for x in range(160, 641, 40):
            draw.rectangle((x, y, x + 3, y + 17), fill=15)
            draw.rectangle((x, y + 14, x + 10, y + 17), fill=15)
    return image


class ImageInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def test_png_with_its_dpi_is_a_t5_page_read_by_ocr(self) -> None:
        doc = heocr.extract(encoded(page_image(), "PNG", dpi=(150, 150)), tesseract=FAKE)
        self.assertEqual(doc.format, "png")
        (page,) = doc.pages
        self.assertEqual((page.page_class, page.route), ("T5", "ocr"))
        self.assertEqual((page.width, page.height), (595.2, 841.92))  # 1240 x 1754 px at 150 dpi, in points
        self.assertNotIn("dpi_assumed", page.flags)
        self.assertEqual([b.text for b in doc.blocks], ["שלום עולם\nשורה", "פסקה"])
        self.assertEqual(doc.page_count, 1)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_missing_dpi_is_assumed_and_flagged(self) -> None:
        doc = heocr.extract(encoded(page_image().convert("RGB"), "JPEG"), tesseract=FAKE)
        self.assertEqual(doc.format, "jpeg")
        self.assertIn("dpi_assumed", doc.pages[0].flags)
        self.assertEqual(doc.pages[0].signals["dpi"], 300.0)

    def test_camera_jpeg_uses_glyph_scale_only_for_recognition(self) -> None:
        from PIL import Image

        from heocr.ocr.tesseract import Tesseract

        image = glyph_page()
        exif = Image.Exif()
        exif[272] = "Invented camera"
        log = Path(self.tmp.name) / "argv.jsonl"
        actual = Tesseract._recognize_call
        inputs = []

        def observe(engine, source, base, dpi, psm):
            with Image.open(source) as raster:
                inputs.append(raster.info["dpi"][0])
            return actual(engine, source, base, dpi, psm)

        with mock.patch.dict(os.environ, {"FAKE_LOG": str(log)}), mock.patch.object(
            Tesseract, "_recognize_call", observe
        ):
            doc = heocr.extract(encoded(image, "JPEG", dpi=(72, 72), exif=exif), tesseract=FAKE, check_numbers=False)
        page, = doc.pages
        self.assertEqual((page.width, page.height), image.size)
        self.assertEqual(page.signals["dpi"], 72)
        self.assertEqual(page.signals["render_dpi"], 72)
        self.assertEqual(page.signals["tesseract_dpi_requested"], 180)
        calls = [argv for line in log.read_text().splitlines() if "--dpi" in (argv := json.loads(line))]
        self.assertTrue(calls)
        self.assertEqual({argv[argv.index("--dpi") + 1] for argv in calls}, {"180"})
        self.assertTrue(all(abs(dpi - 72) < .1 for dpi in inputs))
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_camera_hint_falls_back_without_eligible_metadata_or_glyphs(self) -> None:
        from PIL import Image

        exif = Image.Exif()
        exif[271] = "Invented maker"
        glyphs = glyph_page()
        cases = (
            (glyphs, "JPEG", 72, None),
            (glyphs, "JPEG", 150, exif),
            (page_image(), "JPEG", 72, exif),
            (glyphs, "PNG", 72, exif),
        )
        for image, fmt, dpi, tags in cases:
            with self.subTest(fmt=fmt, dpi=dpi, camera=tags is not None):
                attrs = {"exif": tags} if tags is not None else {}
                doc = heocr.extract(encoded(image, fmt, dpi=(dpi, dpi), **attrs), tesseract=FAKE, check_numbers=False)
                page, = doc.pages
                self.assertEqual(page.route, "ocr")
                self.assertEqual(page.signals["render_dpi"], dpi)
                self.assertNotIn("tesseract_dpi_requested", page.signals)

    def test_every_tiff_frame_is_a_page_and_a_blank_one_is_empty(self) -> None:
        frames = [page_image(), page_image(ink=False), page_image()]
        body = encoded(frames[0], "TIFF", save_all=True, append_images=frames[1:], dpi=(150, 150))
        doc = heocr.extract(body, tesseract=FAKE)
        self.assertEqual(doc.format, "tiff")
        self.assertEqual([p.page_class for p in doc.pages], ["T5", "empty", "T5"])
        self.assertEqual(doc.page_count, 3)
        self.assertEqual(sorted({b.page for b in doc.blocks}), [0, 2])

    def test_fax_compressed_tiff(self) -> None:
        bilevel = page_image().point(lambda v: 0 if v < 128 else 255).convert("1")
        try:
            body = encoded(bilevel, "TIFF", compression="group4", dpi=(200, 200))
        except (OSError, ValueError):
            self.skipTest("this Pillow has no libtiff group4 encoder")
        doc = heocr.extract(body, tesseract=FAKE)
        self.assertEqual(doc.pages[0].page_class, "T5")
        self.assertTrue(doc.blocks)

    def test_an_animated_gif_is_read_as_its_first_frame(self) -> None:
        frames = [page_image(size=(600, 800)).convert("P"), page_image(ink=False, size=(600, 800)).convert("P")]
        body = encoded(frames[0], "GIF", save_all=True, append_images=frames[1:])
        doc = heocr.extract(body, tesseract=FAKE)
        self.assertEqual(len(doc.pages), 1)
        self.assertIn("image_frames_skipped", doc.flags)

    def test_exif_orientation_is_applied(self) -> None:
        from PIL import Image

        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise to view
        body = encoded(page_image().convert("RGB"), "JPEG", exif=exif.tobytes(), dpi=(150, 150))
        doc = heocr.extract(body, tesseract=FAKE)
        page = doc.pages[0]
        self.assertGreater(page.width, page.height)  # the portrait pixels are shown landscape

    def test_transparency_is_flattened_onto_white(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (1240, 1754), (0, 0, 0, 0))  # transparent black: white when shown
        ImageDraw.Draw(image).rectangle([200, 200, 900, 260], fill=(0, 0, 0, 255))
        doc = heocr.extract(encoded(image, "PNG", dpi=(150, 150)), tesseract=FAKE)
        self.assertEqual(doc.pages[0].page_class, "T5")
        blank = Image.new("RGBA", (1240, 1754), (0, 0, 0, 0))
        doc = heocr.extract(encoded(blank, "PNG", dpi=(150, 150)), tesseract=FAKE)
        self.assertEqual(doc.pages[0].page_class, "empty")

    def test_a_ruled_table_in_an_image_is_read_cell_by_cell(self) -> None:
        cells = Path(self.tmp.name) / "cells.json"
        cells.write_text(json.dumps({str(k): v for k, v in TEXTS.items()}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_CELLS": str(cells)}):
            doc = heocr.extract(encoded(scan(LEVELS), "PNG", dpi=(150, 150)), tesseract=FAKE)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in table.cells][0], ["שם", "תפקיד", "סכום"])

    def test_a_huge_image_is_scaled_down_before_ocr(self) -> None:
        with mock.patch("heocr.images.MAX_RENDER_PIXELS", 500_000):
            doc = heocr.extract(encoded(page_image(), "PNG", dpi=(150, 150)), tesseract=FAKE)
        page = doc.pages[0]
        self.assertIn("image_downscaled", page.flags)
        self.assertEqual((page.width, page.height), (595.2, 841.92))  # the page keeps its size in points
        self.assertLess(page.signals["pixels"][0] * page.signals["pixels"][1], 500_001)

    def test_a_broken_image_is_unread(self) -> None:
        doc = heocr.extract(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, tesseract=FAKE)
        self.assertEqual([u.reason for u in doc.unread], ["image_unreadable"])
        self.assertIn("not_read", doc.flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_without_ocr_the_page_is_unread(self) -> None:
        doc = heocr.extract(encoded(page_image(), "PNG", dpi=(150, 150)), ocr=False)
        self.assertEqual([u.reason for u in doc.unread], ["ocr_route_unavailable"])
        self.assertEqual(doc.pages[0].route, "none")


class PageLimitTest(unittest.TestCase):
    """At most max_pages pages or frames are read; the rest are one unread entry, never silent."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def check(self, doc, total: int, limit: int) -> None:
        self.assertEqual(doc.page_count, total)
        self.assertEqual(len(doc.pages), limit)
        limits = [u for u in doc.unread if u.reason == "page_limit"]
        self.assertEqual(len(limits), 1)  # one entry for the whole range
        self.assertEqual(limits[0].detail, f"pages {limit + 1}-{total} of {total} not read (max_pages={limit})")
        self.assertIn("page_limit_reached", doc.flags)
        self.assertEqual(doc.options["max_pages"], limit)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_tiff_frames_beyond_the_limit(self) -> None:
        frames = [page_image(size=(300, 400)) for _ in range(6)]
        body = encoded(frames[0], "TIFF", save_all=True, append_images=frames[1:], dpi=(150, 150))
        log = Path(self.tmp.name) / "calls.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_LOG": str(log)}):
            doc = heocr.extract(body, tesseract=FAKE, max_pages=2)
        self.check(doc, 6, 2)
        ocr_calls = [line for line in log.read_text(encoding="utf-8").splitlines() if "--psm" in line]
        self.assertEqual(len(ocr_calls), 2)  # no OCR beyond the limit

    def test_pdf_pages_beyond_the_limit(self) -> None:
        from tests.synth.pdf import PdfPage, lines_block, make_pdf

        pages = [PdfPage(lines=lines_block([f"עמוד {n}"])) for n in range(1, 6)]
        doc = heocr.extract(make_pdf(pages), ocr=False, max_pages=3)
        self.check(doc, 5, 3)
        self.assertNotIn("עמוד 4", doc.text)

    def test_within_the_limit_nothing_is_flagged(self) -> None:
        doc = heocr.extract(encoded(page_image(), "PNG", dpi=(150, 150)), tesseract=FAKE, max_pages=1)
        self.assertNotIn("page_limit_reached", doc.flags)
        self.assertEqual(doc.options["max_pages"], 1)

    def test_the_default_and_bad_values(self) -> None:
        from heocr.contract import MAX_PAGES

        self.assertEqual(MAX_PAGES, 2000)
        doc = heocr.extract(encoded(page_image(), "PNG", dpi=(150, 150)), ocr=False)
        self.assertEqual(doc.options["max_pages"], 2000)
        for bad in (0, -1, True, 2.5):
            with self.assertRaises(ValueError):
                heocr.extract(b"%PDF-1.4", max_pages=bad)


if __name__ == "__main__":
    unittest.main()
