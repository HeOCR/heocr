"""Every Tesseract call that fails or times out leaves a trace in the record, on every path.

A caller that catches the failure and carries on (a second reading, a check, a cell, the route probe)
used to leave its reading out with no flag, so a slow or paused machine changed the output unseen.

Each path is driven by an existing test that reaches it, run again with the Tesseract calls made from
that path forced to fail: once timing out (``subprocess.TimeoutExpired``), once exiting with an error.
Every document the test reads must then carry the ``ocr_call_failed`` flag and unread entry, and the
page being read the page flag and ``signals.ocr_calls_failed``. The same test, run without a forced
failure, leaves none.
"""

from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import heocr
from heocr.cli import main as cli
from heocr.ocr import tesseract

# (the function the call is made from, its module, an existing test that reaches it, whether the call is
# made while a page is read, so the page itself is flagged)
PATHS = [
    ("_check_ocr_layer", "reader.py", "tests.test_ocr.OcrRouteTest.test_a_partial_ocr_layer_is_flagged", True),
    ("recognize", "reader.py", "tests.test_fontmap.PdfRouteTest.test_shifted_font_is_restored_from_the_page", False),
    ("_check_numbers", "reader.py", "tests.test_number_checks.RouteTest.test_agreement", False),
    ("_number_reading", "reader.py", "tests.test_ocr_numbers.NumberWordsTest.test_a_currency_sign_ends_a_segment", True),
    ("_reread_number_segments", "reader.py", "tests.test_ocr_numbers.NumberWordsTest.test_a_currency_sign_ends_a_segment", True),
    ("_label_from_crops", "reader.py", "tests.test_ocr_numbers.NumberWordsTest.test_a_cut_label_read_as_noise_is_read_again_on_more_crops", True),
    ("_page_number_pass", "reader.py", "tests.test_ocr_numbers.NumberWordsTest.test_a_currency_sign_ends_a_segment", True),
    ("_ocr_page", "reader.py", "tests.test_ocr.OcrRouteTest.test_image_only_page_is_read_by_ocr", True),
    ("_ocr_tables", "reader.py", "tests.test_ocr_tables.OcrTableTest.test_a_cell_whose_two_readings_differ_and_neither_is_confident_is_unconfirmed", True),
    ("_half_number_reading", "reader.py", "tests.test_ocr_tables.OcrTableTest.test_a_cell_whose_two_readings_differ_and_neither_is_confident_is_unconfirmed", True),
    ("_take_half_reading", "reader.py", "tests.test_ocr_tables.OcrTableTest.test_a_number_cell_takes_its_half_resolution_reading_on_its_own_evidence", True),
    ("_second_number_reading", "reader.py", "tests.test_ocr_tables.OcrTableTest.test_a_cell_whose_two_readings_differ_and_neither_is_confident_is_unconfirmed", True),
    ("read_region", "reader.py", "tests.test_ocr.OcrRouteTest.test_a_letterhead_on_every_page_is_read_once_and_kept_on_every_page", True),
    ("_decide", "routes.py", "tests.test_models.AutoRouteTest.test_the_probe_switches_to_a_clearly_better_model", True),
    ("image_blocks", "reader.py", "tests.test_docx_images.DocxImageTest.test_embedded_image_is_read_by_ocr_in_place", False),
    ("_digit_agreement", "reader.py", "tests.test_ocr.NumbersAndCodesLayerTest.test_a_layer_whose_digits_agree_is_kept", True),
    ("_layer_digits", "reader.py", "tests.test_ocr.NumbersAndCodesLayerTest.test_a_layer_of_bare_numbers_is_checked_by_its_digits_alone", True),
]


def _from(function: str, module: str) -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        code = frame.f_code
        if code.co_name == function and code.co_filename.endswith(module):
            return True
        frame = frame.f_back
    return False


def _replay(test_id: str, function: str | None = None, module: str = "", mode: str = "timeout"):
    """Run an existing test, with the recognition calls made from ``function`` forced to fail. Returns
    (the documents it read, with whether a call failed during each, and how many calls were forced)."""
    real_run, real_extract = subprocess.run, heocr.extract
    forced = []
    docs = []

    def run(cmd, *args, **kwargs):
        if function and "tsv" in cmd and _from(function, module):
            forced.append(cmd)
            if mode == "timeout":
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 180)
            return subprocess.CompletedProcess(cmd, 1, b"", b"forced failure")
        return real_run(cmd, *args, **kwargs)

    def extract(*args, **kwargs):
        before = len(forced)
        doc = real_extract(*args, **kwargs)
        docs.append((doc, len(forced) > before))
        return doc

    suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
    with mock.patch.object(tesseract.subprocess, "run", run), mock.patch.object(heocr, "extract", extract):
        suite.run(unittest.TestResult())  # its own assertions may fail now: only the documents matter
    return docs, len(forced)


class OcrCallFailureTest(unittest.TestCase):
    def test_forced_stock_cli_records_engine_and_output_failures(self) -> None:
        from PIL import Image, ImageDraw

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "test.png"
            image = Image.new("RGB", (600, 800), "white")
            ImageDraw.Draw(image).rectangle((30, 30, 550, 65), fill="black")
            image.save(source)
            (folder / "phist_v2.traineddata").write_bytes(b"local fixture model")
            args = [str(source), "--ocr-model", "stock", "--ocr-lang", "phist_v2", "--psm", "3",
                    "--max-pages", "1", "--check-numbers", "off", "--no-convert", "--tesseract", sys.executable,
                    "--format", "json"]
            for mode, reason, detail in (
                ("permission", "ocr_route_unavailable", "could not start tesseract (EACCES)"),
                ("space", "ocr_failed", "could not start tesseract (ENOSPC)"),
                ("timeout", "ocr_failed", "tesseract timed out"),
                ("killed", "ocr_failed", "tesseract terminated by signal 9"),
                ("dimensions", "ocr_failed", "tesseract rejected the image dimensions"),
                ("missing", "ocr_failed", "tesseract produced no TSV output"),
                ("empty", "ocr_failed", "tesseract produced invalid TSV output"),
                ("malformed", "ocr_failed", "tesseract produced invalid TSV output"),
                ("nonfinite", "ocr_failed", "tesseract produced invalid TSV output"),
            ):
                with self.subTest(mode=mode):
                    out, err = io.StringIO(), io.StringIO()

                    def run(argv, **kwargs):
                        if argv[-1] == "--version":
                            if mode == "permission":
                                raise PermissionError(errno.EACCES, "hidden", str(folder))
                            return subprocess.CompletedProcess(argv, 0, b"tesseract 5.5.3\n", b"")
                        self.assertEqual(argv[argv.index("-l") + 1], "phist_v2")
                        if mode == "space":
                            raise OSError(errno.ENOSPC, "hidden", str(folder))
                        if mode == "timeout":
                            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                        if mode == "killed":
                            return subprocess.CompletedProcess(argv, -9, b"", b"")
                        if mode == "dimensions":
                            return subprocess.CompletedProcess(argv, 1, b"", b"Image too large: (32768, 32)")
                        tsv = Path(argv[2]).with_suffix(".tsv")
                        if mode == "empty":
                            tsv.touch()
                        if mode in {"malformed", "nonfinite"}:
                            left = "?" if mode == "malformed" else "NaN"
                            tsv.write_text("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\t"
                                           "top\twidth\theight\tconf\ttext\n"
                                           f"5\t1\t1\t1\t1\t1\t{left}\t2\t18\t23\t90\tabc\n", encoding="utf-8")
                        return subprocess.CompletedProcess(argv, 0, b"", b"")

                    with (mock.patch.dict(os.environ, {"TESSDATA_PREFIX": tmp}),
                          mock.patch.object(tesseract.subprocess, "run", run),
                          redirect_stdout(out), redirect_stderr(err)):
                        self.assertEqual(cli(args), 0)
                    record = json.loads(out.getvalue())
                    self.assertEqual(heocr.validate(record), [])
                    self.assertEqual(record["blocks"], [])
                    self.assertEqual(record["pages"][0]["route"], "none")
                    self.assertEqual(record["unread"][0]["reason"], reason)
                    self.assertIn(detail, record["unread"][0]["detail"])
                    self.assertIn(detail, err.getvalue())
                    self.assertNotIn(tmp, out.getvalue() + err.getvalue())

    def test_every_path_flags_a_timed_out_or_failed_call(self) -> None:
        for function, module, test_id, on_page in PATHS:
            for mode in ("timeout", "failure"):
                with self.subTest(path=function, mode=mode):
                    docs, forced = _replay(test_id, function, module, mode)
                    self.assertGreater(forced, 0, f"{test_id} no longer reaches {function}")
                    failed = [doc for doc, hit in docs if hit]
                    self.assertTrue(failed)
                    for doc in failed:
                        self.assertIn("ocr_call_failed", doc.flags)
                        self.assertTrue(any(u.reason == "ocr_call_failed" for u in doc.unread))
                        if on_page and doc.pages:
                            pages = [p for p in doc.pages if "ocr_call_failed" in p.flags]
                            self.assertTrue(pages, "no page flagged")
                            self.assertTrue(all(p.signals["ocr_calls_failed"] > 0 for p in pages))
                        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_no_forced_failure_no_flag(self) -> None:
        for test_id in sorted({p[2] for p in PATHS}):
            with self.subTest(test=test_id):
                docs, _ = _replay(test_id)
                self.assertTrue(docs)
                for doc, _ in docs:
                    self.assertNotIn("ocr_call_failed", doc.flags)
                    self.assertFalse(any(u.reason == "ocr_call_failed" for u in doc.unread))


if __name__ == "__main__":
    unittest.main()
