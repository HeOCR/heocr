import contextlib
from hashlib import sha256
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw

import heocr
from heocr.cli import main
from heocr.ocr import old_print
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, Tesseract, _temporary_models
from tests.synth.pdf import PdfPage, lines_block, make_pdf
from tests.test_models import FAKE


def image_bytes():
    image = Image.new("L", (650, 320), 255)
    draw = ImageDraw.Draw(image)
    for y in (20, 65, 110, 155, 200):
        for x in range(24, 580, 22):
            draw.rectangle((x, y, x + 15, y + 12), fill=0)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def reading(*, historical=True, numbers=False, confidence=92):
    texts = [
        "רבי " if historical else "",
        "שנאמר " if historical else "",
        "דברים " if historical else "",
        "עברית ",
        "מידע ",
    ]
    return OcrResult([
        OcrLine(prefix + "הטקסט מופיע בשורה לקריאה ולבדיקה ברורה וחוזרת" + (" 1234567890" if numbers else ""),
                [24, 20 + n * 45, 580, 32 + n * 45], [confidence] * 8)
        for n, prefix in enumerate(texts)
    ])


def engine(lang, result):
    result_engine = mock.Mock(spec=Tesseract, lang=lang, psm=3, route=None, failures=0, version="tesseract 5.5.3")
    result_engine.recognize.return_value = result
    result_engine.provenance.return_value = {}
    return result_engine


class HistoricalRouterTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.model = Path(scratch.name) / "local-name.traineddata"
        self.model.write_bytes(b"test weight")
        self.patch_size = mock.patch.object(old_print, "PHIST_V2_SIZE", self.model.stat().st_size)
        self.patch_size.start()
        self.addCleanup(self.patch_size.stop)
        self.opts = {"ocr_router": "v2", "ocr_capture_type": "scanner", "check_numbers": False}
        self.primary = engine("heb_rivok", reading())
        self.historical = engine("phist_v2", reading(confidence=90))

    def test_routes_historical_ocr_and_preserves_model_pin_in_record(self):
        with mock.patch.object(Tesseract, "find", side_effect=[self.primary, self.historical]) as find:
            doc = heocr.extract(image_bytes(), ocr_old_print_model=self.model, **self.opts)
        self.assertEqual(find.call_args.kwargs["local_pin"], (self.model, "phist_v2", old_print.PHIST_V2_SHA256))
        self.assertEqual(doc.pages[0].signals["ocr_old_print"], "phist_v2")
        block = doc.blocks[0].ocr
        self.assertEqual(block["model_route"]["model"], "phist_v2")
        self.assertEqual(block["model_route"]["model_sha256"], old_print.PHIST_V2_SHA256)
        self.assertNotIn(self.model.name, doc.to_json())
        self.assertEqual(doc.components["tesseract:phist_v2.traineddata"], "sha256:" + old_print.PHIST_V2_SHA256)

    def test_modern_and_native_issued_rasters_keep_router_v2_records(self):
        for capture, result in (
            ("scanner", reading(historical=False)),
            ("scanner", reading(numbers=True)),
            ("issuer-render", reading()),
            ("phone", reading(historical=False)),
        ):
            with self.subTest(capture=capture):
                self.primary.recognize.return_value = result
                opts = {**self.opts, "ocr_capture_type": capture}
                with mock.patch.object(Tesseract, "find", return_value=self.primary) as find:
                    baseline = heocr.extract(image_bytes(), **opts)
                    routed = heocr.extract(image_bytes(), ocr_old_print_model=self.model, **opts)
                self.assertEqual(find.call_count, 2)
                record = routed.to_dict()
                record["options"]["ocr"].pop("old_print_model_sha256")
                self.assertEqual(record, baseline.to_dict())

    def test_rejects_sparse_or_weak_historical_reread(self):
        for weak in (reading(confidence=70), reading(historical=False), OcrResult([])):
            with self.subTest(weak=weak):
                self.historical.recognize.return_value = weak
                with mock.patch.object(Tesseract, "find", side_effect=[self.primary, self.historical]):
                    routed = heocr.extract(image_bytes(), ocr_old_print_model=self.model, **self.opts)
                self.assertNotIn("ocr_old_print", routed.pages[0].signals)
                self.assertEqual(routed.blocks[0].ocr["lang"], "heb_rivok")

    def test_bad_pin_or_missing_file_leaves_router_v2_available(self):
        with mock.patch.object(Tesseract, "find", side_effect=[self.primary, OcrUnavailable("bad pin")]):
            routed = heocr.extract(image_bytes(), ocr_old_print_model=self.model, **self.opts)
        self.assertIn("ocr_old_print_model_unavailable", routed.flags)
        self.assertEqual(routed.blocks[0].ocr["lang"], "heb_rivok")
        self.model.unlink()
        with mock.patch.object(Tesseract, "find", return_value=self.primary) as find:
            missing = heocr.extract(image_bytes(), ocr_old_print_model=self.model, **self.opts)
        find.assert_called_once()
        self.assertIn("ocr_old_print_model_unavailable", missing.flags)
        self.assertEqual(missing.text, routed.text)

    def test_text_layer_does_not_load_the_pinned_model(self):
        pdf = make_pdf([PdfPage(lines=lines_block(["יש טקסט קריא במסמך"]))])
        with mock.patch.object(old_print, "_engine") as find:
            doc = heocr.extract(pdf, ocr_old_print_model=self.model, **self.opts)
        find.assert_not_called()
        self.assertEqual(doc.pages[0].route, "text_layer")

    def test_cli_forwards_the_model_and_api_requires_router_v2(self):
        with self.assertRaisesRegex(ValueError, "needs router v2"):
            heocr.extract(image_bytes(), ocr_old_print_model=self.model)
        with mock.patch("heocr.cli.extract", return_value=heocr.extract(b"", ocr=False)) as extract:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["page.png", "--ocr-router", "v2", "--ocr-old-print-model", str(self.model)]), 0)
        self.assertEqual(extract.call_args.kwargs["ocr_old_print_model"], str(self.model))

    def test_staged_model_digest_and_stable_language_label(self):
        configs = self.model.parent / "configs"
        configs.mkdir()
        (configs / "tsv").write_text("", encoding="utf-8")
        digest = sha256(self.model.read_bytes()).hexdigest()
        with _temporary_models():
            staged = Tesseract.find(FAKE, tessdata=self.model.parent, local_pin=(self.model, "phist_v2", digest))
            self.assertEqual(staged.models, {"phist_v2": digest})
            self.assertEqual(staged.lang, "phist_v2")
            self.assertTrue((staged.tessdata / "phist_v2.traineddata").is_file())
            with self.assertRaisesRegex(OcrUnavailable, "staged model pin"):
                Tesseract.find(FAKE, tessdata=self.model.parent, local_pin=(self.model, "phist_v2", "0" * 64))
        self.assertFalse(staged.tessdata.exists())
