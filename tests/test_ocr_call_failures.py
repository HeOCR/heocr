"""Every Tesseract call that fails or times out leaves a trace in the record, on every path.

A caller that catches the failure and carries on (a second reading, a check, a cell, the route probe)
used to leave its reading out with no flag, so a slow or paused machine changed the output unseen.

Each path is driven by an existing test that reaches it, run again with the Tesseract calls made from
that path forced to fail: once timing out (``subprocess.TimeoutExpired``), once exiting with an error.
Every document the test reads must then carry the ``ocr_call_failed`` flag and unread entry, and the
page being read the page flag and ``signals.ocr_calls_failed``. The same test, run without a forced
failure, leaves none.
"""

import subprocess
import sys
import unittest
from unittest import mock

import heocr
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
