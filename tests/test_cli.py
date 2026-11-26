import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from heocr import validate
from heocr.cli import main


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.input = Path(self.tmp.name) / "input.bin"
        self.input.write_bytes(b"not a document")

    def test_json_to_file_by_suffix(self) -> None:
        out = Path(self.tmp.name) / "out.json"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.input), "-o", str(out)]), 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(validate(payload), [])
        self.assertNotIn("input.bin", out.read_text(encoding="utf-8"))
        self.assertIn("unread: format_not_supported", err.getvalue())

    def test_max_pages(self) -> None:
        from tests.synth.pdf import PdfPage, lines_block, make_pdf

        source = Path(self.tmp.name) / "three.pdf"
        source.write_bytes(make_pdf([PdfPage(lines=lines_block([f"עמוד {n}"])) for n in range(1, 4)]))
        out = Path(self.tmp.name) / "out.json"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main([str(source), "--no-ocr", "--max-pages", "1", "-o", str(out)]), 0)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["options"]["max_pages"], 1)
        self.assertIn("page_limit_reached", payload["flags"])
        self.assertIn("unread: page_limit", err.getvalue())

    def test_declared_capture_reaches_extract(self) -> None:
        out = Path(self.tmp.name) / "out.json"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--ocr-capture-type", "issuer-render", "-o", str(out)]), 0)
        ocr = json.loads(out.read_text(encoding="utf-8"))["options"]["ocr"]
        self.assertEqual((ocr["capture_type"], ocr["model"]), ("issuer-render", "heb_rivok"))

    def test_opt_in_router_reaches_extract(self) -> None:
        out = Path(self.tmp.name) / "out.json"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--ocr-router", "v2", "-o", str(out)]), 0)
        ocr = json.loads(out.read_text(encoding="utf-8"))["options"]["ocr"]
        self.assertEqual(ocr["router"], {"version": "v2", "reason": "no_capture_evidence"})
        self.assertEqual(ocr["model"], "heocr_modern_v1")

    def test_photo_preprocessing_cli_forwards_the_mode(self) -> None:
        from unittest import mock

        import heocr.cli

        doc = heocr.cli.extract(self.input, ocr=False)
        for mode in ("gray-otsu", "gray-otsu-dpi-retry"):
            with self.subTest(mode=mode), mock.patch.object(heocr.cli, "extract", return_value=doc) as extract:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main([str(self.input), "--photo-preprocess", mode]), 0)
                self.assertEqual(extract.call_args.kwargs["photo_preprocess"], mode)

    def test_line_batch_cli_forwards_the_switch(self) -> None:
        from unittest import mock

        import heocr.cli

        doc = heocr.cli.extract(self.input, ocr=False)
        with mock.patch.object(heocr.cli, "extract", return_value=doc) as extract, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--ocr-line-v6", "v6.traineddata", "--ocr-line-batch"]), 0)
        self.assertTrue(extract.call_args.kwargs["ocr_line_batch"])
        self.assertEqual(extract.call_args.kwargs["ocr_line_v6"], "v6.traineddata")

    def test_number_batch_cli_forwards_the_switch(self) -> None:
        from unittest import mock

        import heocr.cli

        doc = heocr.cli.extract(self.input, ocr=False)
        with mock.patch.object(heocr.cli, "extract", return_value=doc) as extract, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--ocr-number-batch"]), 0)
        self.assertTrue(extract.call_args.kwargs["ocr_number_batch"])

    def test_quiet_text_to_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.input), "-q"]), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_a_missing_ocr_model_is_reported_even_when_quiet(self) -> None:
        # An install from the repository has no bundled models: stock Tesseract reads instead, and says so.
        from unittest import mock

        import heocr.cli

        doc = heocr.extract(self.input, ocr=False)
        doc.options["ocr"] = {"engine": "tesseract", "lang": "heb", "psm": 3, "model": "heocr_modern_v1"}
        doc.flags.append("bundled_model_unavailable")
        for quiet in ([], ["-q"]):
            with self.subTest(quiet=bool(quiet)):
                err = io.StringIO()
                with mock.patch.object(heocr.cli, "extract", return_value=doc), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    self.assertEqual(main([str(self.input), *quiet]), 0)
                self.assertIn("heocr_modern_v1 is not installed", err.getvalue())
                self.assertIn("falls back to stock Tesseract", err.getvalue())
                self.assertNotIn("scripts/fetch_models.py", err.getvalue())
                self.assertNotIn("PyPI", err.getvalue())

    def test_no_model_warning_when_the_model_is_there(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.input), "--no-ocr"]), 0)
        self.assertNotIn("warning", err.getvalue())

    def test_missing_file(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main([str(Path(self.tmp.name) / "missing.pdf")]), 1)
        self.assertIn("heocr:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
