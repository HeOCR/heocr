"""A second reading of number words on OCR pages (D-bench gap 4: office scans).

Stock ``heb`` misreads digits, drops the levels of a label ("1.1." as "1.") and splits amounts. On
an OCR page, each word that holds digits and no Hebrew letter is read again on its own crop by the
number model (``eng``, ``--psm 8``) and replaced when that reading is number-shaped and confident.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from tests.synth.pdf import PdfPage, make_pdf

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]
HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


class NumberWordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        for lang in ("heb", "eng"):
            (tessdata / f"{lang}.traineddata").write_bytes(f"fake {lang} model".encode())
        self.log = Path(self.tmp.name) / "calls.jsonl"
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata), "FAKE_LOG": str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def page(self, words: list[str]) -> str:
        """A one-line page OCR, right to left: word n at x = 2000 - 300 n."""
        rows = [HEADER] + [f"5\t1\t1\t1\t1\t{n}\t{2000 - 300 * n}\t200\t250\t60\t90.0\t{w}" for n, w in enumerate(words, 1)]
        path = Path(self.tmp.name) / "page.tsv"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return str(path)

    def read(self, words: list[str], second: str, confidence: str = "90.0", **kwargs) -> heocr.Document:
        with mock.patch.dict(os.environ, {"FAKE_TSV": self.page(words), "FAKE_WORD": second, "FAKE_CONF": confidence}):
            return heocr.extract(make_pdf([PdfPage(images=[(0, 0, 595, 842)])]), tesseract=FAKE, **kwargs)

    def second_readings(self) -> list[str]:
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [a[a.index("-l") + 1] for a in calls if "--psm" in a and a[a.index("--psm") + 1] == "8"]

    def test_a_label_misread_by_heb_is_read_again_by_eng(self) -> None:
        doc = self.read(["1.", "תאגיד", "למעט"], "1.1.")
        block = doc.blocks[0]
        self.assertEqual(block.text, "1.1. תאגיד למעט")
        self.assertIn("numbers_from_second_reading", block.flags)
        self.assertEqual(block.ocr["number_words_replaced"], 1)
        self.assertEqual((doc.pages[0].coverage["number_words_reread"], doc.pages[0].coverage["number_words_replaced"]), (1, 1))
        self.assertEqual(self.second_readings(), ["eng"])  # only the digit word, and in eng
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_words_with_hebrew_letters_keep_their_reading(self) -> None:
        doc = self.read(["סעיף", "2ב", "ו-3"], "23")
        self.assertEqual(doc.blocks[0].text, "סעיף 2ב ו-3")
        self.assertEqual(self.second_readings(), [])

    def test_an_unsure_or_unlike_second_reading_is_not_used(self) -> None:
        for second, confidence in (("256,000", "20.0"), ("abc", "90.0"), ("256 000", "90.0")):
            with self.subTest(second):
                doc = self.read(["סכום", "20000"], second, confidence)
                self.assertEqual(doc.blocks[0].text, "סכום 20000")
                self.assertNotIn("numbers_from_second_reading", doc.blocks[0].flags)

    def test_an_amount_is_read_again(self) -> None:
        doc = self.read(["סכום", "20000", "₪"], "256,000")
        self.assertEqual(doc.blocks[0].text, "סכום 256,000 ₪")

    def test_a_label_keeps_its_closing_stop(self) -> None:
        # eng drops a label's closing stop more often than heb invents one.
        doc = self.read(["2.1", "תאגיד"], "2.1.1")
        self.assertEqual(doc.blocks[0].text, "2.1.1 תאגיד")  # no stop read by heb: none added
        doc = self.read(["1.", "תאגיד", "למעט"], "1.2")
        self.assertEqual(doc.blocks[0].text, "1.2. תאגיד למעט")

    def test_a_one_level_label_at_line_start_gets_a_wider_reading(self) -> None:
        # heb read only the last level of "3.2.1."; the word's own crop gives the same, the wider one the label.
        state = str(Path(self.tmp.name) / "seq")
        with mock.patch.dict(os.environ, {"FAKE_WORD_SEQ": "1.|3.2.1.", "FAKE_WORD_STATE": state}):
            doc = self.read(["1.", "תאגיד", "למעט"], "")
        self.assertEqual(doc.blocks[0].text, "3.2.1. תאגיד למעט")
        self.assertEqual(self.second_readings(), ["eng", "eng"])

    def test_a_wider_reading_that_is_not_the_same_label_deeper_is_not_used(self) -> None:
        state = str(Path(self.tmp.name) / "seq")
        for wider in ("3.2.4.", "12,000"):
            with self.subTest(wider), mock.patch.dict(os.environ, {"FAKE_WORD_SEQ": f"1.|{wider}", "FAKE_WORD_STATE": state}):
                Path(state).unlink(missing_ok=True)
                doc = self.read(["1.", "תאגיד"], "")
                self.assertEqual(doc.blocks[0].text, "1. תאגיד")

    def test_no_second_reading_when_number_checks_are_off(self) -> None:
        doc = self.read(["1.", "תאגיד"], "1.1.", check_numbers=False)
        self.assertEqual(doc.blocks[0].text, "1. תאגיד")
        self.assertEqual(self.second_readings(), [])


if __name__ == "__main__":
    unittest.main()
