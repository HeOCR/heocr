"""Opt-in check against a real PDF producer: LibreOffice renders a synthetic DOCX with real fonts.

CI does not run it (no external programs in CI). Run it locally with
``HEOCR_TEST_SOFFICE=1 python -m unittest tests.test_external_producer``.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import heocr
from tests.synth.docx import Docx, para

LINES = [
    "המציע יגיש את הצעתו עד המועד הקבוע",
    'סעיף 3.1 לחוק המכרזים, התשנ"ב-1992',
    'סכום ההצעה 8,500,000 ₪ כולל מע"מ',
    "מיום 15.7.2020 ועד ליום 31.12.2025",
    "נתונים נוספים ניתנים לעיון",
]


@unittest.skipUnless(os.environ.get("HEOCR_TEST_SOFFICE") and shutil.which("soffice"), "opt-in; needs LibreOffice")
class LibreOfficePdfTest(unittest.TestCase):
    def test_libreoffice_pdf_is_t1_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synth.docx"
            source.write_bytes(Docx(body=[para(t) for t in LINES]).build())
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            doc = heocr.extract(Path(tmp) / "synth.pdf")
        self.assertEqual(doc.pages[0].page_class, "T1", doc.pages[0].signals)
        self.assertEqual([line.text for b in doc.blocks for line in b.lines], LINES)


if __name__ == "__main__":
    unittest.main()
