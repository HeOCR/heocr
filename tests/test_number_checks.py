import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from heocr.pdf.numbers import compare, readings, wanted
from tests.synth.pdf import PdfPage, lines_block, make_pdf

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]


class CompareTest(unittest.TestCase):
    def test_lines_worth_checking(self) -> None:
        self.assertTrue(wanted("הסכום 8,500,000 ₪"))
        self.assertTrue(wanted("מיום 15.7.2020"))
        self.assertTrue(wanted("50% מהסכום"))
        self.assertTrue(wanted("הסכום 000,005,8"))
        self.assertFalse(wanted("סעיף 3 לחוק"))

    def test_compare(self) -> None:
        self.assertEqual(compare("8,500,000 ₪ עד 15.7.2020", "8,500,000 ₪ עד 15.7.2020", 95), (2, 2, 0, []))
        self.assertEqual(compare("8,500,000 ₪", "8,500,060 ₪", 95), (1, 0, 1, []))
        # An untrustworthy reading does not contradict the layer: unconfirmed, not disagreed.
        self.assertEqual(compare("8,500,000 ₪", "8,500,060 ₪", 60), (1, 0, 0, []))
        self.assertEqual(compare("8,500,000 עד 30.09", "8,500,000", 95), (2, 1, 0, []))
        self.assertEqual(readings("000,005,8"), ("8,500,000", "8,005,000"))
        self.assertEqual(compare("000,005,8", "8,500,000", 95), (1, 0, 0, [("000,005,8", "8,500,000")]))
        self.assertEqual(compare("000,005,8", "8,005,000", 95), (1, 0, 0, [("000,005,8", "8,005,000")]))
        self.assertEqual(compare("000,005,8", "8,500,001", 95), (1, 0, 1, []))


class RouteTest(unittest.TestCase):
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

    def extract(self, text: str, ocr_line: str, **kwargs):
        kwargs.setdefault("check_numbers", "all")
        with mock.patch.dict(os.environ, {"FAKE_LINE": ocr_line}):
            return heocr.extract(make_pdf([PdfPage(lines=lines_block([text]))]), tesseract=FAKE, **kwargs)

    def test_agreement(self) -> None:
        doc = self.extract("הסכום הוא 8,500,000 ₪", "הסכום הוא 8,500,000 ₪")
        block = doc.blocks[0]
        self.assertEqual(block.checks, {"numbers": 1, "agreed": 1, "disagreed": 0})
        self.assertNotIn("number_disagreement", block.flags)
        self.assertEqual((doc.pages[0].coverage["numbers_checked"], doc.pages[0].coverage["numbers_agreed"]), (1, 1))
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_disagreement_is_flagged_not_resolved(self) -> None:
        doc = self.extract("הסכום הוא 8,500,000 ₪", "הסכום הוא 8,500,060 ₪")
        block = doc.blocks[0]
        self.assertIn("number_disagreement", block.flags)
        self.assertEqual(block.text, "הסכום הוא 8,500,000 ₪")

    def test_low_confidence_reading_leaves_the_number_unconfirmed(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_CONF": "40.0"}):
            doc = self.extract("הסכום הוא 8,500,000 ₪", "הסכום הוא 8,500,060 ₪")
        self.assertIn("numbers_unconfirmed", doc.blocks[0].flags)
        self.assertNotIn("number_disagreement", doc.blocks[0].flags)

    def test_digit_groups_repaired_only_when_confirmed(self) -> None:
        doc = self.extract("הסכום הוא 000,005,8 ₪", "הסכום הוא 8,500,000 ₪")
        block = doc.blocks[0]
        self.assertEqual(block.text, "הסכום הוא 8,500,000 ₪")
        self.assertIn({"rule": "reversed_digit_groups", "from": "000,005,8", "to": "8,500,000", "count": 1}, block.repairs)
        self.assertIn("digit_groups_confirmed", block.flags)
        self.assertNotIn("digit_groups_suspect", block.flags)
        self.assertNotIn("number_disagreement", block.flags)

    def test_unconfirmed_digit_groups_stay_flagged(self) -> None:
        doc = self.extract("הסכום הוא 000,005,8 ₪", "הסכום הוא 8,500,001 ₪")
        block = doc.blocks[0]
        self.assertEqual(block.text, "הסכום הוא 000,005,8 ₪")
        self.assertIn("digit_groups_suspect", block.flags)
        self.assertIn("number_disagreement", block.flags)

    def test_repairs_off_keeps_the_layer(self) -> None:
        doc = self.extract("הסכום הוא 000,005,8 ₪", "הסכום הוא 8,500,000 ₪", repairs=False)
        self.assertEqual(doc.blocks[0].text, "הסכום הוא 000,005,8 ₪")
        self.assertIn("number_disagreement", doc.blocks[0].flags)

    def test_default_scope_reads_only_suspect_groups(self) -> None:
        plain = self.extract("הסכום הוא 8,500,000 ₪", "הסכום הוא 8,500,060 ₪", check_numbers="suspect")
        self.assertIsNone(plain.blocks[0].checks)  # not read a second time by default
        suspect = self.extract("הסכום הוא 000,005,8 ₪", "הסכום הוא 8,500,000 ₪", check_numbers="suspect")
        self.assertEqual(suspect.blocks[0].text, "הסכום הוא 8,500,000 ₪")
        self.assertEqual(heocr.extract(b"x").to_dict()["options"]["check_numbers"], "suspect")
        with self.assertRaises(ValueError):
            heocr.extract(b"x", check_numbers="some")

    def test_check_off_and_no_ocr(self) -> None:
        doc = self.extract("הסכום הוא 8,500,000 ₪", "x", check_numbers=False)
        self.assertIsNone(doc.blocks[0].checks)
        self.assertFalse(doc.to_dict()["options"]["check_numbers"])
        none = heocr.extract(make_pdf([PdfPage(lines=lines_block(["הסכום הוא 8,500,000 ₪"]))]), tesseract=str(Path(self.tmp.name) / "missing"), check_numbers="all")
        self.assertIn("numbers_not_checked", none.flags)


if __name__ == "__main__":
    unittest.main()
