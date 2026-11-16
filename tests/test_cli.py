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

    def test_quiet_text_to_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.input), "-q"]), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_missing_file(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main([str(Path(self.tmp.name) / "missing.pdf")]), 1)
        self.assertIn("heocr:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
