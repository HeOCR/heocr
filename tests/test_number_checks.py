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
        # OCR read only part of a comma-joined token: unconfirmed, not disagreed (dev, all 9 with eng).
        self.assertEqual(compare("עודכן 1:05,28.9.2026", "עודכן 1:05", 95), (1, 0, 0, []))
        self.assertEqual(compare("עמודים 4932,4931", "עמודים 4932", 95), (1, 0, 0, []))
        # The same digits split into tokens differently is no disagreement either.
        self.assertEqual(compare("12,345 67", "12 34567", 95)[2], 0)


class RouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        (tessdata / "eng.traineddata").write_bytes(b"fake eng model")
        self.tessdata = tessdata
        self.log = Path(self.tmp.name) / "calls.jsonl"
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata), "FAKE_LOG": str(self.log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def langs_at_psm7(self) -> list[str]:
        import json

        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []
        return [a[a.index("-l") + 1] for a in calls if "--psm" in a and a[a.index("--psm") + 1] == "7"]

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

    def test_the_second_reading_is_in_eng(self) -> None:
        doc = self.extract("הסכום הוא 8,500,000 ₪", "הסכום הוא 8,500,000 ₪")
        self.assertEqual(self.langs_at_psm7(), ["eng"])
        self.assertEqual(doc.components["tesseract:number_lang"], "eng")
        self.assertTrue(doc.components["tesseract:eng.traineddata"].startswith("sha256:"))
        self.assertNotIn("numbers_read_with_main_model", doc.flags)

    def test_without_eng_the_main_model_reads_and_says_so(self) -> None:
        (self.tessdata / "eng.traineddata").unlink()
        doc = self.extract("הסכום הוא 8,500,000 ₪", "הסכום הוא 8,500,000 ₪")
        self.assertEqual(self.langs_at_psm7(), ["heb"])
        self.assertIn("numbers_read_with_main_model", doc.flags)
        self.assertEqual(doc.blocks[0].checks["agreed"], 1)

    def test_a_footer_keeps_its_number_checks(self) -> None:
        from tests.synth.pdf import TextLine

        pages = [PdfPage(lines=lines_block(["המציע יגיש את הצעתו"], top=700) + [TextLine("עדכון 15.7.2020", 545, 30)]) for _ in range(4)]
        with mock.patch.dict(os.environ, {"FAKE_LINE": "15.7.2020"}):
            doc = heocr.extract(make_pdf(pages), tesseract=FAKE, check_numbers="all")
        footers = [b for b in doc.blocks if b.role == "footer"]
        self.assertEqual(len(footers), 4)
        self.assertTrue(all(b.checks == {"numbers": 1, "agreed": 1, "disagreed": 0} for b in footers))


class TableCellTest(unittest.TestCase):
    """D3d inside table cells read from the layer: checked and repaired like paragraph lines, logged on the table."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        for lang in ("heb", "eng"):
            (tessdata / f"{lang}.traineddata").write_bytes(f"fake {lang} model".encode())
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def extract(self, amount: str, ocr_line: str, **kwargs):
        from tests.test_pdf_tables import table_page

        rows = [["סכום", "תפקיד", "שם"], [amount, "מנהל", "ספק א"], ["", "עובד", "ספק ב"]]
        with mock.patch.dict(os.environ, {"FAKE_LINE": ocr_line, "FAKE_CONF": "95.0"}):
            return heocr.extract(make_pdf([table_page(rows)]), tesseract=FAKE, **kwargs)

    def table(self, doc):
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        return table

    def test_a_reversed_amount_in_a_cell_is_repaired_on_confirmation(self) -> None:
        table = self.table(self.extract("000,005,8", "8,500,000"))
        self.assertEqual(table.cells[1][2].text, "8,500,000")
        self.assertIn({"rule": "reversed_digit_groups", "from": "000,005,8", "to": "8,500,000", "count": 1}, table.repairs)
        self.assertIn("digit_groups_confirmed", table.flags)
        self.assertEqual(table.checks, {"numbers": 1, "agreed": 1, "disagreed": 0})

    def test_a_disagreeing_cell_is_flagged_not_changed(self) -> None:
        doc = self.extract("15.7.2020", "15.7.2021", check_numbers="all")
        table = self.table(doc)
        self.assertEqual(table.cells[1][2].text, "15.7.2020")
        self.assertIn("number_disagreement", table.flags)
        self.assertIn("number_disagreement", table.cells[1][2].flags)
        self.assertEqual(doc.pages[0].coverage["numbers_disagreed"], 1)

    def test_the_tables_scope_reads_cells_not_lines(self) -> None:
        from tests.test_pdf_tables import table_page

        rows = [["סכום", "תפקיד", "שם"], ["15.7.2020", "מנהל", "ספק א"], ["", "עובד", "ספק ב"]]
        page = table_page(rows, extra=lines_block(["מיום 15.7.2020 ועד היום"], top=800))
        with mock.patch.dict(os.environ, {"FAKE_LINE": "15.7.2020", "FAKE_CONF": "95.0"}):
            doc = heocr.extract(make_pdf([page]), tesseract=FAKE, check_numbers="tables")
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual(table.checks, {"numbers": 1, "agreed": 1, "disagreed": 0})
        self.assertTrue(all(b.checks is None for b in doc.blocks if b.kind == "text"))  # lines: suspect only
        self.assertEqual(doc.options["check_numbers"], "tables")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_with_repairs_off_the_cell_stays_as_extracted(self) -> None:
        table = self.table(self.extract("000,005,8", "8,500,000", repairs=False))
        self.assertEqual(table.cells[1][2].text, "000,005,8")
        self.assertIn("number_disagreement", table.flags)


if __name__ == "__main__":
    unittest.main()
