"""The per-document time limit (D5c): a safety net, off by default, that never drops what it leaves unread."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from heocr import contract
from heocr.cli import main
from tests.synth.docx import Docx, image_run, para
from tests.synth.pdf import PdfPage, lines_block, make_pdf
from tests.test_docx_images import png
from tests.test_images import encoded, page_image

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]


def three_pages() -> bytes:
    return make_pdf([PdfPage(lines=lines_block([f"עמוד מספר {n}"])) for n in range(1, 4)])


class Clock:
    """A clock that moves one second each time it is read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += 1.0
        return value


class TimeLimitTest(unittest.TestCase):
    def test_off_by_default(self) -> None:
        doc = heocr.extract(three_pages(), ocr=False)
        self.assertNotIn("time_limit", doc.options)
        self.assertNotIn("time_limit_reached", doc.flags)
        self.assertEqual(len(doc.pages), 3)

    def test_a_generous_limit_changes_nothing(self) -> None:
        free = heocr.extract(three_pages(), ocr=False)
        limited = heocr.extract(three_pages(), ocr=False, time_limit=3600)
        self.assertEqual(limited.text, free.text)
        self.assertEqual(limited.unread, free.unread)
        self.assertEqual(limited.options["time_limit"], 3600)
        self.assertEqual(heocr.validate(limited.to_dict()), [])

    def test_the_pages_left_are_unread_never_dropped(self) -> None:
        # Read at 0 s (start), then before each page at 1, 2 and 3 s: a 2.5 s limit stops before page 3.
        with mock.patch.object(contract, "_now", Clock()):
            doc = heocr.extract(three_pages(), ocr=False, time_limit=2.5)
        self.assertEqual(len(doc.pages), 2)
        self.assertEqual(doc.page_count, 3)
        self.assertEqual([(u.scope, u.reason) for u in doc.unread], [("document", "time_limit")])
        self.assertEqual(doc.unread[0].detail, "page 3 of 3 not read: the time limit of 2.5 s was reached")
        self.assertEqual(doc.flags.count("time_limit_reached"), 1)
        self.assertNotIn("עמוד מספר 3", doc.text)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_range_of_pages(self) -> None:
        with mock.patch.object(contract, "_now", Clock()):
            doc = heocr.extract(three_pages(), ocr=False, time_limit=1.5)
        self.assertEqual(len(doc.pages), 1)
        self.assertEqual(doc.unread[0].detail, "pages 2-3 of 3 not read: the time limit of 1.5 s was reached")

    def test_invalid_limits_are_refused(self) -> None:
        for bad in (0, -1, float("inf"), float("nan"), True, "60"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                heocr.extract(three_pages(), ocr=False, time_limit=bad)

    def test_a_record_with_a_bad_limit_does_not_validate(self) -> None:
        payload = heocr.extract(three_pages(), ocr=False, time_limit=10).to_dict()
        payload["options"]["time_limit"] = 0
        self.assertTrue(any("time_limit" in error for error in heocr.validate(payload)))

    def test_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "three.pdf", Path(tmp) / "out.json"
            source.write_bytes(three_pages())
            err = io.StringIO()
            with contextlib.redirect_stderr(err), mock.patch.object(contract, "_now", Clock()):
                self.assertEqual(main([str(source), "--no-ocr", "--time-limit", "1.5", "-o", str(out)]), 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["options"]["time_limit"], 1.5)
        self.assertIn("time_limit_reached", payload["flags"])
        self.assertIn("unread: time_limit", err.getvalue())


class TimeLimitOcrTest(unittest.TestCase):
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

    def test_image_frames_left_are_unread(self) -> None:
        frames = [page_image(size=(300, 400)) for _ in range(3)]
        body = encoded(frames[0], "TIFF", save_all=True, append_images=frames[1:], dpi=(150, 150))
        log = Path(self.tmp.name) / "calls.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_LOG": str(log)}), mock.patch.object(contract, "_now", Clock()):
            doc = heocr.extract(body, tesseract=FAKE, time_limit=2.5)
        self.assertEqual(len(doc.pages), 2)
        self.assertEqual(doc.unread[-1].detail, "page 3 of 3 not read: the time limit of 2.5 s was reached")
        ocr_calls = [line for line in log.read_text(encoding="utf-8").splitlines() if "--psm" in line]
        self.assertEqual(len(ocr_calls), 2)  # no OCR after the limit
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_an_embedded_image_after_the_limit_is_unread(self) -> None:
        body = Docx(images=[png(), png()], body=[para(image_run(1)), para("ביניים"), para(image_run(2))]).build()
        with mock.patch.object(contract, "_now", Clock()):
            doc = heocr.extract(body, tesseract=FAKE, time_limit=1.5)
        self.assertEqual([b.kind for b in doc.blocks][-1], "figure")
        self.assertIn("ביניים", doc.text)
        self.assertEqual([(u.scope, u.reason) for u in doc.unread], [("block", "time_limit")])
        self.assertEqual(doc.unread[0].detail, "an embedded image not read: the time limit of 1.5 s was reached")
        self.assertIn("time_limit_reached", doc.flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_numbers_left_unchecked_are_flagged_not_unread(self) -> None:
        # Two text-layer pages, read at 1 and 2 s; the number check would start at 3 s, after a 2.5 s limit.
        pdf = make_pdf([PdfPage(lines=lines_block([f"הסכום הוא 000,005,{n} ₪"])) for n in (8, 9)])
        log = Path(self.tmp.name) / "calls.jsonl"
        with mock.patch.dict(os.environ, {"FAKE_LOG": str(log)}), mock.patch.object(contract, "_now", Clock()):
            doc = heocr.extract(pdf, tesseract=FAKE, time_limit=2.5)
        self.assertEqual(len(doc.pages), 2)
        self.assertIn("numbers_not_checked", doc.flags)
        self.assertIn("time_limit_reached", doc.flags)
        self.assertNotIn("time_limit", [u.reason for u in doc.unread])
        self.assertFalse(log.exists() and any("--psm" in line for line in log.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(heocr.validate(doc.to_dict()), [])


if __name__ == "__main__":
    unittest.main()
