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


if __name__ == "__main__":
    unittest.main()
