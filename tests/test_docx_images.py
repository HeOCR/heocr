import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import heocr
from tests.synth.docx import Docx, image_run, para

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]


def png(size=(600, 200), ink=True, mode="L") -> bytes:
    image = Image.new(mode, size, 255 if mode == "L" else (255, 255, 255, 0))
    if ink:
        ImageDraw.Draw(image).rectangle([20, 20, size[0] - 20, 60], fill=0 if mode == "L" else (0, 0, 0, 255))
    out = io.BytesIO()
    image.save(out, format="PNG", dpi=(200, 200))
    return out.getvalue()


class DocxImageTest(unittest.TestCase):
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

    def extract(self, images, body, **kwargs):
        return heocr.extract(Docx(images=images, body=body).build(), tesseract=FAKE, **kwargs)

    def test_embedded_image_is_read_by_ocr_in_place(self) -> None:
        doc = self.extract([png()], [para("לפני"), para(image_run(1)), para("אחרי")])
        self.assertEqual([b.text for b in doc.blocks], ["לפני", "שלום עולם\nשורה", "פסקה", "אחרי"])
        block = doc.blocks[1]
        self.assertEqual(block.source, "ocr")
        self.assertIn("from_embedded_image", block.flags)
        self.assertEqual(block.ocr["dpi"], 200)
        self.assertEqual(doc.unread, [])
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_transparent_logo_is_flattened_on_white(self) -> None:
        doc = self.extract([png(mode="RGBA")], [para(image_run(1))])
        self.assertEqual(doc.blocks[0].source, "ocr")

    def test_blank_small_and_unsupported_images_are_reported(self) -> None:
        images = [png(ink=False), png(size=(10, 10), ink=False), b"\x01\x00\x00\x00 EMF-like bytes"]
        doc = self.extract(images, [para(image_run(1)), para(image_run(2)), para(image_run(3))])
        self.assertEqual([u.reason for u in doc.unread], ["image_blank", "image_too_small", "image_format_not_supported"])
        self.assertEqual([b.kind for b in doc.blocks], ["figure"] * 3)

    def test_ocr_off_or_missing_keeps_the_figure_unread(self) -> None:
        doc = self.extract([png()], [para(image_run(1))], ocr=False)
        self.assertEqual((doc.blocks[0].kind, doc.unread[0].reason, doc.unread[0].detail), ("figure", "image_not_read", "ocr turned off"))

    def test_low_confidence_is_reported_not_emitted(self) -> None:
        tsv = Path(self.tmp.name) / "low.tsv"
        tsv.write_text(
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t10\t20\t20\t12.0\t:\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"FAKE_TSV": str(tsv)}):
            doc = self.extract([png()], [para(image_run(1))])
        self.assertEqual([u.reason for u in doc.unread], ["image_ocr_low_confidence"])

    def test_low_confidence_paragraphs_inside_a_good_image_are_left_out(self) -> None:
        tsv = Path(self.tmp.name) / "mixed.tsv"
        tsv.write_text(
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t10\t200\t20\t95.0\tכותרת\n"
            "5\t1\t2\t1\t1\t1\t10\t80\t200\t20\t20.0\tפופו\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"FAKE_TSV": str(tsv)}):
            doc = self.extract([png()], [para(image_run(1))])
        self.assertEqual([b.text for b in doc.blocks], ["כותרת"])
        self.assertEqual(doc.unread[0].detail, "1 of 2 paragraphs below confidence 50")

    def test_huge_image_is_scaled_under_the_cap(self) -> None:
        from heocr.docx.reader import _open_image

        with mock.patch("heocr.docx.reader.MAX_RENDER_PIXELS", 50_000):
            image, dpi, _ = _open_image(png(size=(1000, 400)))
        self.assertLessEqual(image.size[0] * image.size[1], 50_000)
        self.assertLess(dpi, 200)


if __name__ == "__main__":
    unittest.main()
