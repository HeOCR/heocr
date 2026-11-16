"""D4a: DOC, RTF and ODT converted to DOCX by LibreOffice and read by our own reader.

The fake soffice (``tests/synth/fake_soffice.py``) stands in for LibreOffice, so the safety rules
(isolated profile, timeout, process group, never the text export) are tested offline.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import heocr
from heocr import convert
from tests.synth.cfb import make_cfb
from tests.synth.docx import Docx, para

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_soffice.py")]
DOC = make_cfb(["WordDocument", "1Table"])
RTF = b"{\\rtf1\\ansi {\\rtlch \\u1513?\\u1500?\\u1493?\\u1501?}}"


def odt() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", "<office:document-content/>")
    return out.getvalue()


class ConverterTest(unittest.TestCase):
    def setUp(self) -> None:
        convert._IDENTITIES.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docx = Path(self.tmp.name) / "converted.docx"
        self.docx.write_bytes(Docx(body=[para("שלום עולם"), para("סעיף שני")], footnotes={1: "הערה"}).build())
        self.log = Path(self.tmp.name) / "calls.jsonl"
        patcher = mock.patch.dict(os.environ, {"FAKE_SOFFICE_DOCX": str(self.docx), "FAKE_SOFFICE_LOG": str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_doc_rtf_and_odt_are_converted_and_read(self) -> None:
        for name, body in (("doc", DOC), ("rtf", RTF), ("odt", odt())):
            with self.subTest(name):
                doc = heocr.extract(body, soffice=FAKE)
                self.assertEqual(doc.format, name)
                self.assertEqual([b.text for b in doc.blocks if b.role == "body"], ["שלום עולם", "סעיף שני"])
                self.assertEqual({b.source for b in doc.blocks}, {"converted_docx"})
                self.assertIn("הערה", [b.text for b in doc.blocks if b.role == "footnote"])  # notes survive
                self.assertIn("converted_by_libreoffice", doc.flags)
                self.assertEqual(doc.components["libreoffice"], "LibreOffice 7.9.9.9")
                self.assertEqual(doc.options["convert"], {"engine": "libreoffice", "to": "docx"})
                self.assertEqual(doc.unread, [])
                self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_every_call_has_its_own_profile_and_never_exports_text(self) -> None:
        heocr.extract(DOC, soffice=FAKE)
        heocr.extract(RTF, soffice=FAKE)
        calls = self.calls()
        profiles = [a for call in calls for a in call if a.startswith("-env:UserInstallation=file:")]
        self.assertEqual(len(profiles), len(calls))
        self.assertEqual(len(set(profiles)), len(calls))  # a fresh profile every time
        conversions = [c for c in calls if "--convert-to" in c]
        self.assertEqual([c[c.index("--convert-to") + 1] for c in conversions], ["docx:MS Word 2007 XML"] * 2)
        self.assertTrue(all("--headless" in c for c in conversions))
        self.assertFalse(any("txt" in a for c in calls for a in c))
        self.assertEqual(sum("--version" in c for c in calls), 1)  # the version is found once

    def test_a_hanging_converter_is_stopped_with_its_children(self) -> None:
        pidfile = Path(self.tmp.name) / "child.pid"
        with mock.patch.dict(os.environ, {"FAKE_SOFFICE_HANG": "1", "FAKE_SOFFICE_PIDFILE": str(pidfile)}):
            start = time.monotonic()
            doc = heocr.extract(DOC, soffice=FAKE, convert_timeout=3)
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 30)
        self.assertEqual([u.reason for u in doc.unread], ["conversion_timeout"])
        self.assertIn("not_read", doc.flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        if sys.platform != "win32":
            child = int(pidfile.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(child):
                time.sleep(0.1)
            self.assertFalse(_alive(child), "the converter's child process was left running")

    def test_a_failed_conversion_is_unread_with_its_reason(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_SOFFICE_FAIL": "1"}):
            doc = heocr.extract(DOC, soffice=FAKE)
        self.assertEqual([u.reason for u in doc.unread], ["conversion_failed"])
        self.assertIn("could not be loaded", doc.unread[0].detail)
        with mock.patch.dict(os.environ, {"FAKE_SOFFICE_GARBAGE": "1"}):
            doc = heocr.extract(DOC, soffice=FAKE)
        self.assertEqual([u.reason for u in doc.unread], ["conversion_failed"])
        self.assertEqual(doc.blocks, [])

    def test_without_libreoffice_or_with_conversion_off(self) -> None:
        missing = heocr.extract(DOC, soffice=str(Path(self.tmp.name) / "no-such-soffice"))
        self.assertEqual([u.reason for u in missing.unread], ["converter_unavailable"])
        off = heocr.extract(DOC, convert=False)
        self.assertEqual([u.reason for u in off.unread], ["converter_unavailable"])
        self.assertIn("conversion turned off", off.unread[0].detail)
        self.assertIs(off.options["convert"], False)
        self.assertEqual(self.calls(), [])
        for doc in (missing, off):
            self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_pdf_and_docx_are_not_converted(self) -> None:
        doc = heocr.extract(self.docx.read_bytes(), soffice=FAKE)
        self.assertNotIn("convert", doc.options)
        self.assertEqual(self.calls(), [])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (exited, not yet reaped by its parent) counts as stopped.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
            return stream.read().split()[2] != "Z"
    except OSError:
        import subprocess

        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")


if __name__ == "__main__":
    unittest.main()
