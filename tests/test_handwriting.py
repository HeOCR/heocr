"""Offline routing tests with invented scan ink and Kraken records."""

import contextlib
from io import BytesIO, StringIO
from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import heocr
from heocr.cli import main
from heocr.handwriting import HandwritingReader
from heocr.ocr.tesseract import OcrLine, OcrResult
from tests.synth.pdf import PdfPage, lines_block, make_pdf


PIN = "a" * 64
HANDWRITTEN = {"type": [{"type": "handwriting"}]}
PRINTED = {"type": [{"type": "printed"}]}


def invented_scan():
    image = Image.new("L", (400, 250), 255)
    draw = ImageDraw.Draw(image)
    for y in (35, 85):
        for x in range(85, 303, 14):
            draw.line([(x, y + 7), (x + 5, y), (x + 8, y + 12)], fill=0, width=3)
    output = BytesIO()
    image.save(output, format="PNG", dpi=(144, 144))
    return output.getvalue()


def config(mode="force"):
    if mode == "auto":
        return heocr.HandwritingConfig(
            "/local/recognizer.safetensors",
            PIN,
            mode,
            "/local/segmenter.safetensors",
            "b" * 64,
        )
    return heocr.HandwritingConfig("/local/recognizer.safetensors", PIN, mode)


def geometry_config():
    return heocr.HandwritingConfig(
        "/local/recognizer.safetensors", PIN, detector="geometry"
    )


def backend(tags=(HANDWRITTEN, HANDWRITTEN), records=None):
    engine = Mock()
    engine.segment.return_value = SimpleNamespace(
        lines=[SimpleNamespace(id=str(i), tags=t) for i, t in enumerate(tags)]
    )
    if records is None:
        # Deliberately reverse record arrival. Segmenter order determines the output.
        records = [
            SimpleNamespace(
                id="1",
                prediction="מילים נוספות",
                bbox=(78, 82, 315, 103),
                confidences=[0.8],
            ),
            SimpleNamespace(
                id="0",
                prediction="כיתוב ראשון 17",
                boundary=[(80, 33), (314, 36), (311, 53), (78, 50)],
                confidences=[0.9, 0.7],
            ),
        ]
    engine.recognize.return_value = records
    return engine


class HandwritingTest(unittest.TestCase):
    def test_opt_in_auto_image_maps_lines_in_segmentation_order(self):
        engine = backend()
        with (
            patch("heocr.handwriting._load", return_value=engine) as load,
            patch(
                "heocr.pdf.reader.engine_for",
                side_effect=AssertionError("ordinary OCR must not run"),
            ),
        ):
            doc = heocr.extract(invented_scan(), handwriting=config("auto"))
        load.assert_called_once()
        view = engine.segment.call_args.args[0]
        self.assertEqual(view.size, (400, 400))
        self.assertEqual(view.getpixel((200, 399)), 255)
        (page,) = doc.pages
        self.assertEqual((page.route, page.page_class), ("ocr", "T5"))
        self.assertIn("handwriting_kraken", page.flags)
        self.assertEqual(
            (page.signals["ocr_engine"], page.coverage["status"]), ("kraken", "ocr")
        )
        self.assertEqual(
            [b.text for b in doc.blocks], ["כיתוב ראשון 17", "מילים נוספות"]
        )
        self.assertEqual(doc.blocks[0].lines[0].bbox, [39, 16.5, 157, 26.5])
        self.assertEqual(doc.blocks[0].ocr["mean_confidence"], 80)
        self.assertEqual(doc.blocks[0].ocr["status"], "unverified")
        self.assertEqual(doc.components["kraken:recognizer"], f"sha256:{PIN}")
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        self.assertNotIn("/local/", doc.to_json())

    def test_force_scan_pdf_routes_untyped_lines_with_local_model(self):
        data = make_pdf([PdfPage(images=[(0, 0, 595, 842)], image_gray=64)])
        engine = backend(tags=(None, None))
        with (
            patch("heocr.handwriting._load", return_value=engine),
            patch(
                "heocr.pdf.reader.engine_for",
                side_effect=AssertionError("ordinary OCR must not run"),
            ),
        ):
            doc = heocr.extract(data, handwriting=config())
        self.assertEqual(doc.pages[0].page_class, "T5")
        self.assertIn("handwriting_kraken", doc.pages[0].flags)
        self.assertIn("ocr_short_for_ink", doc.pages[0].flags)
        self.assertEqual(doc.pages[0].coverage["status"], "partial")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_auto_declines_mixed_or_untyped_pages(self):
        for tags in ((HANDWRITTEN, PRINTED), (HANDWRITTEN, None)):
            with self.subTest(tags=tags):
                engine = backend(tags=tags)
                with patch("heocr.handwriting._load", return_value=engine):
                    self.assert_ordinary_ocr(config("auto"), "handwriting_not_detected")
                engine.recognize.assert_not_called()

    def test_geometry_rejects_before_loading_kraken(self):
        with (
            patch("heocr.handwriting._detect_geometry", return_value=False) as detect,
            patch("heocr.handwriting._load") as load,
        ):
            self.assert_ordinary_ocr(None)
            detect.assert_not_called()
            self.assert_ordinary_ocr(geometry_config(), "handwriting_not_detected")
            detect.assert_called_once()
            load.assert_not_called()

    def test_geometry_accepts_untyped_segmenter_lines(self):
        engine = backend(tags=(None, None))
        with (
            patch("heocr.handwriting._detect_geometry", return_value=True),
            patch("heocr.handwriting._load", return_value=engine) as load,
        ):
            doc = heocr.extract(invented_scan(), handwriting=geometry_config())
        load.assert_called_once()
        engine.recognize.assert_called_once()
        self.assertIn("handwriting_kraken", doc.pages[0].flags)
        self.assertEqual(doc.options["handwriting"]["detector"], "geometry-v1")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_geometry_unavailable_falls_back_without_loading_kraken(self):
        with (
            patch("heocr.handwriting._detect_geometry", side_effect=ImportError),
            patch("heocr.handwriting._load") as load,
        ):
            self.assert_ordinary_ocr(
                geometry_config(), "handwriting_detector_unavailable"
            )
            load.assert_not_called()

    @unittest.skipUnless(find_spec("numpy") and find_spec("scipy"), "optional geometry")
    def test_regular_invented_glyphs_take_ordinary_ocr(self):
        from heocr.handwriting_geometry import handwriting_score, is_handwritten

        image = Image.new("L", (900, 1200), 255)
        draw = ImageDraw.Draw(image)
        for y in range(200, 1000, 31):
            for x in range(160, 755, 26):
                draw.rectangle((x, y, x + 2, y + 10), fill=0)
                draw.rectangle((x, y + 8, x + 7, y + 10), fill=0)
        score = handwriting_score(image)
        self.assertIsNotNone(score)
        self.assertEqual(score, handwriting_score(image))
        self.assertFalse(is_handwritten(image))
        self.assertFalse(is_handwritten(Image.new("L", image.size, 255)))

    def assert_ordinary_ocr(self, handwriting, flag=None):
        engine = Mock()
        engine.lang = "heb"
        engine.psm = 3
        engine.version = "tesseract invented"
        engine.route = None
        engine.recognize.return_value = OcrResult(
            [
                OcrLine("הטקסט הרגיל", [75, 30, 320, 105], [80]),
            ]
        )
        with (
            patch("heocr.pdf.reader.engine_for", return_value=engine),
            patch("heocr.pdf.reader.number_engine_for", return_value=None),
        ):
            doc = heocr.extract(
                invented_scan(), handwriting=handwriting, check_numbers=False
            )
        self.assertEqual(doc.text.strip(), "הטקסט הרגיל")
        self.assertEqual(doc.blocks[0].ocr["engine"], "tesseract")
        if flag:
            self.assertIn(flag, doc.pages[0].flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        return doc

    def test_disabled_and_missing_backend_fallback(self):
        with patch(
            "heocr.handwriting._load", side_effect=RuntimeError("/private/model/path")
        ) as load:
            off = self.assert_ordinary_ocr(None)
            load.assert_not_called()
            on = self.assert_ordinary_ocr(config(), "handwriting_backend_unavailable")
        self.assertNotIn("handwriting", off.options)
        self.assertNotIn("/private/", on.to_json())

    def test_incomplete_read_preserves_ordinary_text(self):
        engine = backend(
            records=[
                SimpleNamespace(
                    id="0",
                    prediction="קריאה חלקית",
                    bbox=(78, 33, 315, 53),
                    confidences=[0.8],
                ),
            ]
        )
        with patch("heocr.handwriting._load", return_value=engine):
            self.assert_ordinary_ocr(config(), "handwriting_incomplete")

    def test_wrong_checkpoint_digest_falls_back_before_inference(self):
        with TemporaryDirectory() as directory:
            model = Path(directory) / "invented.safetensors"
            model.write_bytes(b"invented model bytes")
            with patch("heocr.handwriting.version", return_value="7.2"):
                doc = self.assert_ordinary_ocr(
                    heocr.HandwritingConfig(model, PIN, "force"),
                    "handwriting_backend_unavailable",
                )
        self.assertNotIn("kraken", doc.components)

    def test_multiple_scan_frames_reuse_one_backend(self):
        data = BytesIO()
        with Image.open(BytesIO(invented_scan())) as image:
            image.save(data, format="TIFF", save_all=True, append_images=[image.copy()])
        engine = backend(tags=(None, None))
        with patch("heocr.handwriting._load", return_value=engine) as load:
            doc = heocr.extract(data.getvalue(), handwriting=config())
        load.assert_called_once()
        self.assertEqual([page.index for page in doc.pages], [0, 1])
        self.assertEqual([block.page for block in doc.blocks], [0, 0, 1, 1])
        self.assertEqual(engine.recognize.call_count, 2)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_wide_input_exceeding_padding_budget_skips_backend(self):
        image = Image.new("L", (7000, 50), 255)
        doc = heocr.Document("", 0, "png", "invented")
        with patch("heocr.handwriting._load") as load:
            self.assertEqual(
                HandwritingReader(config()).read(doc, image),
                ("handwriting_input_too_wide", []),
            )
        load.assert_not_called()

    def test_text_layer_stays_eligible_for_its_original_route(self):
        data = make_pdf([PdfPage(lines=lines_block(["טקסט מקורי בקובץ"]))])
        with patch("heocr.handwriting._load") as load:
            doc = heocr.extract(data, handwriting=config(), check_numbers=False)
        load.assert_not_called()
        self.assertEqual(doc.pages[0].route, "text_layer")
        self.assertEqual(doc.text.strip(), "טקסט מקורי בקובץ")

    def test_cli_opt_in_and_invalid_settings(self):
        stdout, stderr = StringIO(), StringIO()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "scan.png"
            path.write_bytes(invented_scan())
            with (
                patch(
                    "heocr.handwriting._load", return_value=backend(tags=(None, None))
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                args = [
                    str(path),
                    "-f",
                    "json",
                    "--handwriting",
                    "invented.safetensors",
                    "--handwriting-sha256",
                    PIN,
                    "--handwriting-mode",
                    "force",
                ]
                self.assertEqual(main(args), 0)
                self.assertEqual(
                    json.loads(stdout.getvalue())["blocks"][0]["ocr"]["engine"],
                    "kraken",
                )
                self.assertEqual(main(args + ["--no-ocr"]), 1)
        self.assertIn("handwriting needs OCR", stderr.getvalue())

    def test_cli_geometry_accepts_scan_without_custom_segmenter(self):
        stdout = StringIO()
        with (
            TemporaryDirectory() as directory,
            patch("heocr.handwriting._detect_geometry", return_value=True) as detect,
            patch("heocr.handwriting._load", return_value=backend(tags=(None, None))),
            contextlib.redirect_stdout(stdout),
        ):
            path = Path(directory) / "scan.png"
            path.write_bytes(invented_scan())
            self.assertEqual(
                main(
                    [
                        str(path),
                        "-f",
                        "json",
                        "--handwriting",
                        "invented.safetensors",
                        "--handwriting-sha256",
                        PIN,
                        "--handwriting-detector",
                        "geometry",
                    ]
                ),
                0,
            )
        detect.assert_called_once()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["options"]["handwriting"]["detector"], "geometry-v1")
        self.assertEqual(output["blocks"][0]["ocr"]["engine"], "kraken")


if __name__ == "__main__":
    unittest.main()
