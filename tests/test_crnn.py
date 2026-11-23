import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import heocr
from heocr.ocr.crnn import CrnnReader, ctc_characters, input_width, line_box
from tests.synth.pdf import PdfPage, lines_block, make_pdf


class CrnnTest(unittest.TestCase):
    def test_crop_and_ctc_alignment(self):
        self.assertEqual(line_box(Image.new("L", (100, 75)), [-0.5, 3.1, 105, 9.6], 2), (0, 6, 100, 20))
        self.assertEqual([input_width(w, h) for w, h in [(1, 400), (13, 8), (900, 1)]], [4, 52, 2048])
        self.assertEqual(ctc_characters([0, 1, 1, 0, 1, 2, 2, 0], ("ב", "א")), "בבא")

    def test_model_checksum_precedes_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "reader.onnx"
            model.write_bytes(b"placeholder")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                CrnnReader(model, "0" * 64)
            with self.assertRaisesRegex(ValueError, "supply both"):
                heocr.extract(b"x", crnn_model=model)
            with self.assertRaisesRegex(ValueError, "needs Hebrew OCR"):
                heocr.extract(b"x", ocr=False, crnn_model=model, crnn_sha256="0" * 64)

    def test_extract_preserves_layout_and_repair_switch(self):
        fake = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]
        scan = PdfPage(images=[(0, 0, 595, 842)], image_gray=0xFF)
        digital = PdfPage(lines=lines_block(["בגינה צומחים פרחים", "החלון פונה מערבה"]))
        pdf = make_pdf([scan, digital])
        options = {"tesseract": fake, "ocr_model": "heb", "check_numbers": False}
        raw = "המחברת נפתחה ,וירד גשם"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FAKE_TESSDATA": directory}):
            os.environ.pop("TESSDATA_PREFIX", None)
            (Path(directory) / "heb.traineddata").write_bytes(b"fake heb model")
            baseline = heocr.extract(pdf, **options)
            with patch("heocr.ocr.crnn.CrnnReader") as constructor:
                reader = constructor.return_value
                reader.recognize.return_value = raw
                reader.version = "onnxruntime test"
                reader.sha256 = "0" * 64
                backend = {"crnn_model": "local.onnx", "crnn_sha256": reader.sha256}
                direct = heocr.extract(pdf, **options, **backend, repairs=False)
                repaired = heocr.extract(pdf, **options, **backend)

        for old, new in zip(baseline.blocks, direct.blocks, strict=True):
            self.assertEqual(old.bbox, new.bbox)
            self.assertEqual(old.page, new.page)
            self.assertEqual([line.bbox for line in old.lines or []], [line.bbox for line in new.lines or []])
            if old.source == "text_layer":
                self.assertEqual(old, new)
            else:
                self.assertEqual(new.ocr["engine"], "crnn")
                self.assertTrue(all(line.text == raw for line in new.lines))
                self.assertIsNone(new.checks)
        self.assertEqual(repaired.blocks[0].lines[0].text, "המחברת נפתחה, וירד גשם")
        self.assertNotIn("local.onnx", repaired.to_json())
        self.assertEqual(heocr.validate(repaired.to_dict()), [])
