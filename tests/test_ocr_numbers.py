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
from tests.synth.fake_tesseract import NOTHING
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

    def page(self, words: list[str], gaps: list[int] | None = None) -> str:
        """A one-line page OCR, right to left: words 250 px wide, 50 px apart (or ``gaps``, px)."""
        gaps = gaps or [50] * len(words)
        rows, right = [HEADER], 2250
        for n, w in enumerate(words, 1):
            left = right - 250
            rows.append(f"5\t1\t1\t1\t1\t{n}\t{left}\t200\t250\t60\t90.0\t{w}")
            right = left - gaps[n - 1]
        path = Path(self.tmp.name) / "page.tsv"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return str(path)

    def read(self, words: list[str], second: str, confidence: str = "90.0", gaps: list[int] | None = None, **kwargs) -> heocr.Document:
        with mock.patch.dict(os.environ, {"FAKE_TSV": self.page(words, gaps), "FAKE_WORD": second or NOTHING, "FAKE_CONF": confidence}):
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
        # Each replacement is logged in its block with both readings, so it can be audited or reverted.
        self.assertEqual(block.checks["number_readings"], [{"first": "1.", "second": "1.1.", "first_confidence": 90.0,
                                                            "second_confidence": 90.0, "model": "eng", "wider": False}])
        self.assertEqual(doc.to_dict()["blocks"][0]["checks"]["number_readings"][0]["first"], "1.")
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
                self.assertIsNone(doc.blocks[0].checks)

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
        [logged] = doc.blocks[0].checks["number_readings"]
        self.assertEqual((logged["first"], logged["second"], logged["wider"]), ("1.", "3.2.1.", True))

    def read_lines(self, lines: list[list[str]], answers: list[str], confidence: str = "8.0") -> heocr.Document:
        """A page of several lines, right to left, 100 px apart; one-word calls answer ``answers`` in turn."""
        rows = [HEADER]
        for ln, words in enumerate(lines, 1):
            right = 2250
            for n, w in enumerate(words, 1):
                rows.append(f"5\t1\t1\t1\t{ln}\t{n}\t{right - 250}\t{100 * ln}\t250\t60\t90.0\t{w}")
                right -= 300
        path = Path(self.tmp.name) / "lines.tsv"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        state = str(Path(self.tmp.name) / "seq-lines")
        Path(state).unlink(missing_ok=True)
        env = {"FAKE_TSV": str(path), "FAKE_WORD_SEQ": "|".join(a or NOTHING for a in answers), "FAKE_WORD_STATE": state, "FAKE_CONF": confidence}
        with mock.patch.dict(os.environ, env):
            return heocr.extract(make_pdf([PdfPage(images=[(0, 0, 595, 842)])]), tesseract=FAKE)

    def test_a_cut_label_read_whole_with_low_confidence_is_used(self) -> None:
        # set C pdf_t3_pua_font: heb reads "2.2.4." at a line's start as "4."; eng reads the same crop
        # "2.2.4" but with confidence 8. A deeper label that ends with heb's level and follows the
        # label before it ("2.2.") is used anyway.
        doc = self.read_lines([["2.2.", "הגדרות"], ["4.", "דיווח", "עיתי"]], ["2.2", "2.2.4"])
        self.assertEqual(doc.text.splitlines(), ["2.2. הגדרות", "2.2.4. דיווח עיתי"])
        [logged] = [r for b in doc.blocks for r in (b.checks or {}).get("number_readings", [])]
        self.assertEqual((logged["first"], logged["second"], logged["label_levels"]), ("4.", "2.2.4.", True))
        self.assertNotIn("used", logged)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_label_missing_its_first_level_is_read_whole(self) -> None:
        # set C pdf_t5_office: heb reads "11.1.2." as "1.2." (confidence 20); eng reads "11.1.2" at 25.
        doc = self.read_lines([["11.1.", "הגדרות"], ["1.2.", "תחולה"]], ["11.1", "11.1.2"])
        self.assertEqual(doc.text.splitlines(), ["11.1. הגדרות", "11.1.2. תחולה"])
        # The reading must keep all of heb's levels at its end: "1.1.3" for "1.2." is not used.
        doc = self.read_lines([["11.1.", "הגדרות"], ["1.2.", "תחולה"]], ["11.1", "1.1.3"])
        self.assertEqual(doc.text.splitlines(), ["11.1. הגדרות", "1.2. תחולה"])

    def test_a_cut_label_out_of_sequence_is_not_used_and_is_logged(self) -> None:
        # "23.2" between "3.1." and "3.3." follows neither: heb's "2." stays.
        doc = self.read_lines([["3.1.", "אחת"], ["2.", "שתיים"], ["3.3.", "שלוש"]], ["3.1", "23.2", "3.3"])
        self.assertEqual(doc.text.splitlines(), ["3.1. אחת", "2. שתיים", "3.3. שלוש"])
        [logged] = [r for b in doc.blocks for r in (b.checks or {}).get("number_readings", [])]
        self.assertEqual((logged["first"], logged["second"], logged["used"]), ("2.", "23.2.", False))
        self.assertNotIn("numbers_from_second_reading", [f for b in doc.blocks for f in b.flags])
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_cut_label_with_no_known_neighbour_is_not_used(self) -> None:
        doc = self.read(["4.", "דיווח", "עיתי"], "2.2.4", "8.0")
        self.assertEqual(doc.blocks[0].text, "4. דיווח עיתי")

    def page_pass(self, lines: list[list[str]], tokens: list[tuple[str, str, list[tuple[int, int]]]]) -> heocr.Document:
        """``lines`` read by heb as in ``read_lines``; every crop's second reading finds nothing, and the
        number model's whole-page reading holds ``tokens``: (text, confidence, the (line, word) boxes of
        heb's it spans, both 1-based). The page pass reads at half the render's resolution."""
        boxes = {}
        for ln, words in enumerate(lines, 1):
            for n in range(1, len(words) + 1):
                left = 2250 - 250 - 300 * (n - 1)
                boxes[(ln, n)] = (left, 100 * ln, left + 250, 100 * ln + 60)
        rows = [HEADER]
        for n, (text, conf, over) in enumerate(tokens, 1):
            x0, y0 = min(boxes[k][0] for k in over), min(boxes[k][1] for k in over)
            x1, y1 = max(boxes[k][2] for k in over), max(boxes[k][3] for k in over)
            rows.append(f"5\t1\t1\t1\t{n}\t1\t{x0 // 2}\t{y0 // 2}\t{(x1 - x0) // 2}\t{(y1 - y0) // 2}\t{conf}\t{text}")
        path = Path(self.tmp.name) / "page-eng.tsv"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_PAGE_TSV_ENG": str(path), "FAKE_WORD": NOTHING, "FAKE_LINE": NOTHING}):
            doc = self.read_lines(lines, [""])
        # Every crop re-read finds nothing here, so any change is the page pass's own: a crop answered
        # with the page's reading (as when Windows dropped an empty $FAKE_WORD) could not pass for it.
        others = [r for b in doc.blocks for r in (b.checks or {}).get("number_readings") or [] if not r.get("page_pass")]
        self.assertEqual(others, [])
        words = sum(len(ws) for ws in lines)
        self.assertLessEqual(sum(len(line.text.split()) for b in doc.blocks for line in b.lines or []), words)  # never adds a word
        return doc

    def page_readings(self, doc: heocr.Document) -> list[dict]:
        return [r for b in doc.blocks for r in (b.checks or {}).get("number_readings") or [] if r.get("page_pass")]

    def test_the_page_pass_restores_a_cut_label_that_follows_its_neighbours(self) -> None:
        # set B pdf_t5_scan: heb reads "2.2.4." as "4.", and the number model reads heb's crop of "4." as
        # nothing; its whole-page reading has the label whole. It ends with heb's level and follows
        # "2.2.", so it is used, at any confidence.
        doc = self.page_pass([["2.2.", "הגדרות"], ["4.", "דיווח"]], [("2.2.", "70", [(1, 1)]), ("2.2.4.", "20", [(2, 1)])])
        self.assertEqual(doc.blocks[0].text.splitlines()[1], "2.2.4. דיווח")
        (logged,) = self.page_readings(doc)
        self.assertEqual((logged["first"], logged["second"], logged["label_levels"]), ("4.", "2.2.4.", True))
        self.assertNotIn("used", logged)
        self.assertEqual(doc.pages[0].coverage["page_number_words_replaced"], 1)

    def agreeing(self, crop: str, token: str) -> heocr.Document:
        """One line whose cut label "4." has no neighbour to vouch for it: the word crop reads it as
        ``crop`` at a low confidence (so the sequence guard turns it down), and the number model's
        whole-page reading has ``token`` over it. All values are invented."""
        lines = [["4.", "דיווח", "עיתי"]]
        left = 2250 - 250
        path = Path(self.tmp.name) / "page-eng-agree.tsv"
        path.write_text(f"{HEADER}\n5\t1\t1\t1\t1\t1\t{left // 2}\t50\t125\t30\t20\t{token}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_PAGE_TSV_ENG": str(path)}):
            return self.read_lines(lines, [crop, "", "", "", "", ""])

    def test_a_cut_label_two_readings_agree_on_is_used(self) -> None:
        # set C office scans: heb cuts "2.2.4." to "4." on a page whose labels are all cut, so no
        # neighbour vouches; the word's crop and the whole page, read apart, both say "2.2.4.".
        doc = self.agreeing("2.2.4", "2.2.4.")
        self.assertEqual(doc.blocks[0].text, "2.2.4. דיווח עיתי")
        readings = [r for b in doc.blocks for r in (b.checks or {}).get("number_readings") or []]
        crop = [r for r in readings if not r.get("page_pass")]
        page = [r for r in readings if r.get("page_pass")]
        self.assertEqual([(r["second"], r.get("used")) for r in crop], [("2.2.4.", False)])  # the crop's own reading, turned down
        self.assertEqual([(r["first"], r["second"], r.get("agreed")) for r in page], [("4.", "2.2.4.", True)])

    def test_cut_label_readings_that_disagree_are_not_used(self) -> None:
        doc = self.agreeing("2.2.4", "2.3.4.")
        self.assertEqual(doc.blocks[0].text, "4. דיווח עיתי")
        self.assertFalse(any(r.get("agreed") for b in doc.blocks for r in (b.checks or {}).get("number_readings") or []))

    def test_agreement_never_overrides_a_known_neighbour(self) -> None:
        # set B pdf_t5_scan: both readings saw a stray mark beside "2." as "9.2.", after a known "1.2.".
        # It cannot follow "1.2.", so it stays out however the readings agree.
        lines = [["1.2.", "הגדרות"], ["2.", "דיווח"]]
        path = Path(self.tmp.name) / "page-eng-contra.tsv"
        path.write_text(f"{HEADER}\n5\t1\t1\t1\t1\t1\t1000\t100\t125\t30\t20\t9.2.\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_PAGE_TSV_ENG": str(path)}):
            doc = self.read_lines(lines, ["1.2", "9.2", "", "", "", ""])
        self.assertEqual(doc.blocks[0].text.splitlines()[1], "2. דיווח")
        self.assertFalse(any(r.get("agreed") for b in doc.blocks for r in (b.checks or {}).get("number_readings") or []))

    def test_no_agreement_when_it_is_off(self) -> None:
        with mock.patch("heocr.pdf.reader.LABEL_AGREEMENT", False):
            doc = self.agreeing("2.2.4", "2.2.4.")
        self.assertEqual(doc.blocks[0].text, "4. דיווח עיתי")

    def test_the_page_pass_label_out_of_sequence_is_logged_and_not_used(self) -> None:
        doc = self.page_pass([["3.1.", "אחת"], ["2.", "שתיים"], ["3.3.", "שלוש"]], [("23.2.", "90", [(2, 1)])])
        self.assertEqual(doc.blocks[0].text.splitlines()[1], "2. שתיים")
        (logged,) = self.page_readings(doc)
        self.assertEqual((logged["second"], logged["used"]), ("23.2.", False))

    def test_the_page_pass_joins_a_split_amount(self) -> None:
        doc = self.page_pass([["סכום", "385", ",000", "ש\"ח"]], [("385,000", "90", [(1, 2), (1, 3)])])
        self.assertEqual(doc.blocks[0].text, "סכום 385,000 ש\"ח")
        (logged,) = self.page_readings(doc)
        self.assertEqual((logged["first"], logged["second"]), ("385 ,000", "385,000"))

    def test_the_page_pass_never_adds_text(self) -> None:
        # eng finds numbers in Hebrew words, stamps and rulings: a token over no number piece of heb's is dropped.
        doc = self.page_pass([["תקנות", "הביטוח"]], [("1994", "95", [(1, 1)]), ("12", "95", [(1, 2)])])
        self.assertEqual(doc.blocks[0].text, "תקנות הביטוח")
        self.assertEqual(self.page_readings(doc), [])

    def test_an_unsure_or_digit_dropping_page_token_is_not_used(self) -> None:
        doc = self.page_pass([["סכום", "385", ",000"]], [("385,000", "30", [(1, 2), (1, 3)])])
        self.assertEqual(doc.blocks[0].text, "סכום 385 ,000")
        doc = self.page_pass([["סכום", "1385", ",000"]], [("385,000", "90", [(1, 2), (1, 3)])])
        self.assertEqual(doc.blocks[0].text, "סכום 1385 ,000")

    def test_a_page_token_adds_digits_only_as_a_date_or_amount(self) -> None:
        # set B pdf_t5_scan: eng's page reading joins nearby ink to a plain number ("60" as "6019").
        doc = self.page_pass([["סעיף", "60", "לחוק"]], [("6019", "90", [(1, 2)])])
        self.assertEqual(doc.blocks[0].text, "סעיף 60 לחוק")
        # set C office_numbers: a date heb read as its year alone is restored.
        doc = self.page_pass([["עד", "2022", "יום"]], [("01.12.2022", "90", [(1, 2)])])
        self.assertEqual(doc.blocks[0].text, "עד 01.12.2022 יום")

    def test_a_page_token_changes_heb_digits_only_when_surer(self) -> None:
        # At 150 dpi eng reads 3 as 5: heb's "354" at 90 stays against eng's "554" at 67.
        doc = self.page_pass([["סעיף", "354", "לחוק"]], [("554", "67", [(1, 2)])])
        self.assertEqual(doc.blocks[0].text, "סעיף 354 לחוק")
        doc = self.page_pass([["סעיף", "354", "לחוק"]], [("554", "95", [(1, 2)])])
        self.assertEqual(doc.blocks[0].text, "סעיף 554 לחוק")

    def test_no_page_pass_when_it_is_off(self) -> None:
        with mock.patch("heocr.pdf.reader.PAGE_NUMBER_PASS", False):
            doc = self.page_pass([["2.2.", "הגדרות"], ["4.", "דיווח"]], [("2.2.4.", "90", [(2, 1)])])
        self.assertEqual(doc.blocks[0].text.splitlines()[1], "4. דיווח")
        self.assertNotIn("page_number_tokens", doc.pages[0].coverage)

    def test_cut_labels_with_no_known_neighbour_vouch_for_each_other_in_strict_sequence(self) -> None:
        # set B pdf_t5_scan: on a page whose labels heb all cut, no known label anchors the guard. Two
        # neighbouring candidates in strict sequence ("1.1.1" then "1.1.2") are used; the first is logged
        # chained, and the second then follows it.
        doc = self.read_lines([["1.", "הגדרות"], ["2.", "תחולה"]], ["1.1.1", "1.1.2"])
        self.assertEqual([l.split()[0] for l in doc.blocks[0].text.splitlines()], ["1.1.1.", "1.1.2."])
        logged = doc.blocks[0].checks["number_readings"]
        self.assertEqual([r.get("chained", False) for r in logged], [True, False])  # the second follows the first, now used
        # A loose step ("3.1.8" then "3.2.9": a new section that does not restart at 1) vouches for nothing.
        doc = self.read_lines([["8.", "הגדרות"], ["9.", "תחולה"]], ["3.1.8", "3.2.9"])
        self.assertEqual([l.split()[0] for l in doc.blocks[0].text.splitlines()], ["8.", "9."])
        with mock.patch("heocr.pdf.reader.LABEL_CHAIN", False):
            doc = self.read_lines([["1.", "הגדרות"], ["2.", "תחולה"]], ["1.1.1", "1.1.2"])
        self.assertEqual([l.split()[0] for l in doc.blocks[0].text.splitlines()], ["1.", "2."])

    def test_label_starts_numbering(self) -> None:
        from heocr.pdf.reader import _label_starts_numbering as starts

        self.assertTrue(starts((1, 1, 1), (1, 1, 2)))
        self.assertTrue(starts((2, 2), (2, 2, 1)))
        self.assertTrue(starts((2, 1, 3), (2, 2)))
        self.assertTrue(starts((2, 1, 3), (2, 2, 1)))
        self.assertFalse(starts((3, 1, 8), (3, 2, 9)))
        self.assertFalse(starts((2, 2), (2, 2, 4)))
        self.assertFalse(starts((11, 1), (12, 2)))

    def test_a_cut_label_read_as_noise_is_read_again_on_more_crops(self) -> None:
        # set C pdf_t5_office: heb read ".1.1.1" as "1.", and the number model read heb's box as noise
        # ("iWeeg"). Crops reaching left, and stretched, read the label among noise ("Yo.1.1.1"); the
        # deeper label they read, ending with heb's level, goes to the guard and follows "1.1.".
        with mock.patch.dict(os.environ, {"FAKE_LINE": "Yo.1.1.1"}):
            doc = self.read_lines([["1.1.", "הגדרות"], ["1.", "תחולה"]], ["1.1", NOTHING])
        self.assertEqual(doc.blocks[0].text.splitlines()[1].split()[0], "1.1.1.")
        (logged,) = [r for r in doc.blocks[0].checks["number_readings"] if r["first"] == "1."]
        self.assertEqual((logged["second"], logged["label_levels"]), ("1.1.1.", True))
        # A label that does not end with heb's level is no candidate; nothing is used with the switch off.
        with mock.patch.dict(os.environ, {"FAKE_LINE": "Yo.1.1.2"}):
            doc = self.read_lines([["1.1.", "הגדרות"], ["1.", "תחולה"]], ["1.1", NOTHING])
        self.assertEqual(doc.blocks[0].text.splitlines()[1].split()[0], "1.")
        with mock.patch.dict(os.environ, {"FAKE_LINE": "Yo.1.1.1"}), mock.patch("heocr.pdf.reader.LABEL_CROPS", False):
            doc = self.read_lines([["1.1.", "הגדרות"], ["1.", "תחולה"]], ["1.1", NOTHING])
        self.assertEqual(doc.blocks[0].text.splitlines()[1].split()[0], "1.")

    def test_crop_candidates_vouch_for_each_other_only_when_most_crops_agree(self) -> None:
        from heocr.ocr.tesseract import OcrLine, OcrWord
        from heocr.pdf.reader import _label_sequence

        def lines():
            out = []
            for n, (label, text) in enumerate((("1.", "הגדרות"), ("2.", "תחולה"))):
                words = [OcrWord(label, [2000, 100 * n, 2050, 100 * n + 40], 90.0), OcrWord(text, [1800, 100 * n, 1990, 100 * n + 40], 90.0)]
                out.append(OcrLine(" ".join(w.text for w in words), [1800, 100 * n, 2050, 100 * n + 40], words=words))
            return out

        for share, used in ((20.0, False), (80.0, True)):
            with self.subTest(share=share):
                ls = lines()
                pending = {id(ls[0].words[0]): ("1.1.1.", share, False, True), id(ls[1].words[0]): ("1.1.2.", share, False, True)}
                _label_sequence(ls, pending, "eng")
                self.assertEqual([l.words[0].text for l in ls], ["1.1.1.", "1.1.2."] if used else ["1.", "2."])
                self.assertTrue(all(r.get("crops") for l in ls for r in l.number_readings))

    def test_crop_candidates_vouch_for_each_other_through_the_cut_labels_between(self) -> None:
        # set C pdf_t5_office: on a page whose labels heb all cut, two crop candidates few crops agree on
        # ("1.4." and "1.4.2.") stand two lines apart, with a cut label between that heb read as "1.".
        # Stepping strictly through it (1.4, then 1.4.1, then 1.4.2) one reaches the other: both are used.
        from heocr.ocr.tesseract import OcrLine, OcrWord
        from heocr.pdf.reader import _label_sequence

        def lines(labels):
            out = []
            for n, label in enumerate(labels):
                words = [OcrWord(label, [2000, 100 * n, 2050, 100 * n + 40], 90.0), OcrWord("סעיף", [1800, 100 * n, 1990, 100 * n + 40], 90.0)]
                out.append(OcrLine(" ".join(w.text for w in words), [1800, 100 * n, 2050, 100 * n + 40], words=words))
            return out

        for cut, used in (("1.", True), ("3.", False)):
            with self.subTest(cut=cut):
                ls = lines(["4.", cut, "2."])
                pending = {id(ls[0].words[0]): ("1.4.", 40.0, False, True), id(ls[2].words[0]): ("1.4.2.", 20.0, False, True)}
                _label_sequence(ls, pending, "eng")
                self.assertEqual([l.words[0].text for l in ls], ["1.4.", cut, "1.4.2."] if used else ["4.", cut, "2."])
                if used:  # the first is vouched for through the cut label; the second then follows it
                    self.assertEqual([r.get("through_cut_labels", False) for l in ls for r in l.number_readings], [True, False])
        with mock.patch("heocr.pdf.reader.LABEL_CARRY", False):
            ls = lines(["4.", "1.", "2."])
            _label_sequence(ls, {id(ls[0].words[0]): ("1.4.", 40.0, False, True), id(ls[2].words[0]): ("1.4.2.", 20.0, False, True)}, "eng")
        self.assertEqual([l.words[0].text for l in ls], ["4.", "1.", "2."])

    def test_label_steps_and_reaches(self) -> None:
        from heocr.pdf.reader import _label_reaches as reaches
        from heocr.pdf.reader import _label_steps as steps

        self.assertEqual(steps((1, 4), 1), {(1, 4, 1), (2, 1)})
        self.assertEqual(steps((1, 3, 2), 4), {(1, 4)})
        self.assertEqual(steps((1, 1, 1), 2), {(2,), (1, 2), (1, 1, 2)})
        self.assertEqual(steps((2, 1, 3), 1), {(2, 1, 3, 1), (2, 2, 1), (3, 1), (3, 1, 1)})
        self.assertTrue(reaches((1, 4), (1, 4, 2), [1]))
        self.assertTrue(reaches((1, 4), (1, 5), [1, None, 2]))  # a line without a label is passed over
        self.assertFalse(reaches((1, 4), (1, 4, 2), [3]))
        self.assertFalse(reaches((1, 4), (1, 4, 2), []))  # with no cut label between, nothing new vouches
        self.assertFalse(reaches((1, 4), (1, 4, 2), [None]))

    def test_label_follows(self) -> None:
        from heocr.pdf.reader import _label_follows as follows
        self.assertTrue(follows((2, 2), (2, 2, 4)))
        self.assertTrue(follows((2, 1, 3), (2, 2)))
        self.assertTrue(follows((2, 3, 5), (3, 1)))
        self.assertTrue(follows((3, 1), (3, 2, 6)))
        self.assertFalse(follows((3, 1), (23, 2)))
        self.assertFalse(follows((2, 2), (2, 2)))
        self.assertFalse(follows((2, 2), (2, 4)))

    def test_a_low_confidence_reading_is_used_only_as_the_same_label_deeper(self) -> None:
        for words, second in ((["4.", "דיווח"], "2.2.5"), (["4.", "דיווח"], "12,000"), (["4.", "דיווח"], "4"),
                              (["סעיף", "4."], "2.2.4"), (["4", "דיווח"], "2.4")):
            with self.subTest(words=words, second=second):
                doc = self.read(words, second, "8.0")
                self.assertEqual(doc.blocks[0].text, " ".join(words))
                self.assertIsNone(doc.blocks[0].checks)

    def test_a_wider_reading_that_is_not_the_same_label_deeper_is_not_used(self) -> None:
        state = str(Path(self.tmp.name) / "seq")
        for wider in ("3.2.4.", "12,000"):
            with self.subTest(wider), mock.patch.dict(os.environ, {"FAKE_WORD_SEQ": f"1.|{wider}", "FAKE_WORD_STATE": state}):
                Path(state).unlink(missing_ok=True)
                doc = self.read(["1.", "תאגיד"], "")
                self.assertEqual(doc.blocks[0].text, "1. תאגיד")

    def test_a_number_split_into_pieces_is_read_again_whole(self) -> None:
        # set C office scans: heb split "25.10.2022" into "2022" and "25."; each piece alone cannot be
        # restored. Adjacent pieces of digits and number punctuation are read again as one crop.
        doc = self.read(["מועד", "2022", "25."], "25.10.2022", gaps=[60, 8, 60])  # the pieces 8 px apart
        block = doc.blocks[0]
        self.assertEqual(block.text, "מועד 25.10.2022")
        (logged,) = block.checks["number_readings"]
        self.assertEqual((logged["first"], logged["second"], logged["segment"]), ("2022 25.", "25.10.2022", 2))
        self.assertEqual(self.second_readings(), [])  # read once as a line (psm 7), not piece by piece (psm 8)

    def test_a_run_reading_that_drops_digits_is_not_used(self) -> None:
        state = str(Path(self.tmp.name) / "seq")
        # The run's reading loses digits ("25.10" for "2022 25."): its pieces are read one by one instead.
        with mock.patch.dict(os.environ, {"FAKE_WORD_SEQ": "25.10|2022|25.", "FAKE_WORD_STATE": state}):
            doc = self.read(["מועד", "2022", "25."], "", gaps=[60, 8, 60])
        self.assertEqual(doc.blocks[0].text, "מועד 2022 25.")
        self.assertEqual(self.second_readings(), ["eng", "eng", "eng"])

    def test_two_numbers_in_neighbouring_columns_stay_two(self) -> None:
        # An unruled table on whole-page OCR: "120,960" and "26.09.2023" a column gap apart are two
        # numbers, never joined, whatever the number model would read over both.
        # Each word read alone gives itself back; a (wrong) joined read would take the first answer,
        # an amount, and replace both words with it.
        state = str(Path(self.tmp.name) / "seq")
        with mock.patch.dict(os.environ, {"FAKE_WORD_SEQ": "120,960|26.09.2023", "FAKE_WORD_STATE": state}):
            doc = self.read(["פריט", "120,960", "26.09.2023"], "", gaps=[20, 200, 20])
        self.assertEqual(doc.blocks[0].text, "פריט 120,960 26.09.2023")
        self.assertFalse(any((r.get("run") or 0) > 1 for r in (doc.blocks[0].checks or {}).get("number_readings", [])))

    def test_a_joined_reading_that_is_not_a_date_or_amount_is_not_used(self) -> None:
        # Close pieces read as "1234": not a date or a grouped amount, so the pieces stay apart.
        state = str(Path(self.tmp.name) / "seq")
        with mock.patch.dict(os.environ, {"FAKE_WORD_SEQ": "1234|12|34", "FAKE_WORD_STATE": state}):
            doc = self.read(["סעיף", "12", "34"], "", gaps=[60, 8, 60])
        self.assertEqual(doc.blocks[0].text, "סעיף 12 34")

    def segment(self, words, line, gaps=None, spans=None, confs=None):
        env = {"FAKE_LINE": line}
        if spans:
            env["FAKE_LINE_SPANS"] = spans
        if confs:
            env["FAKE_CONFS"] = confs
        with mock.patch.dict(os.environ, env):
            return self.read(words, "", gaps=gaps)

    def test_a_table_rows_date_broken_into_marks_is_read_again_as_a_segment(self) -> None:
        # set C office_numbers: heb reads a row's last cell "01.12.2022" as "2022 >": the day and month
        # turned into a mark. The row's number segment is read again by the number model as one line;
        # its date token, over "2022" and ">", replaces both.
        doc = self.segment(["הסעות", "21", "5,990", "2022", ">"], "01.12.2022 5,990 21", spans="0-0.49,0.51-0.75,0.77-0.99")
        block = doc.blocks[0]
        self.assertEqual(block.text, "הסעות 21 5,990 01.12.2022")
        (logged,) = block.checks["number_readings"]
        self.assertEqual((logged["segment"], logged["replaced"]), (4, [["2022 >", "01.12.2022"]]))

    def test_a_mark_the_number_model_cannot_read_leaves_its_word_as_read(self) -> None:
        # set C: the number model read a row's quantity "7" as "V/" at confidence 5: that word stays as heb
        # read it, while the row's date is still restored.
        doc = self.segment(["ליווי", "7", "2021", ">"], "26.10.2021 V/", spans="0-0.66,0.68-0.99", confs="97,5")
        self.assertEqual(doc.blocks[0].text, "ליווי 7 26.10.2021")

    def test_a_date_read_partly_as_a_shekel_sign_is_restored(self) -> None:
        # set C: heb read "26.05.2024" as "05.2024. ₪": part of the date became a ₪. A date token may take its place.
        doc = self.segment(["אחזקה", "50,170", "05.2024.", "₪"], "26.05.2024 50,170", spans="0-0.65,0.68-0.99")
        self.assertEqual(doc.blocks[0].text, "אחזקה 50,170 26.05.2024")

    def test_an_amount_keeps_its_shekel_sign(self) -> None:
        # Only a date takes a ₪'s place: an amount token over a ₪ leaves the ₪ (and the amount) as read.
        doc = self.segment(["סכום", "64,20", "₪"], "64,200", spans="0-0.99")
        self.assertEqual(doc.blocks[0].text, "סכום 64,20 ₪")

    def test_segment_pieces(self) -> None:
        from heocr.pdf.reader import _segment_piece as piece
        for text in ("01.12.2022", ">", "₪", "(₪)", "0ך", "2ךד"):
            self.assertTrue(piece(text), text)
        for text in ("2ב", "ו-3", "של", "20000₪", "3ab", "1ךדה"):
            self.assertFalse(piece(text), text)

    def test_a_misread_piece_with_a_final_form_is_part_of_a_segment(self) -> None:
        # "0ך" beside digits is no Hebrew word: a misread piece of "19.04.2020".
        doc = self.segment(["ייעוץ", "42,120", "0ך", ">"], "19.04.2020 42,120", spans="0-0.65,0.68-0.99")
        self.assertEqual(doc.blocks[0].text, "ייעוץ 42,120 19.04.2020")

    def test_a_segments_numbers_stay_separate_words(self) -> None:
        # Numbers in neighbouring columns come back as separate tokens: never joined into one.
        doc = self.segment(["פריט", "120,960", "26", "09.2023"], "26.09.2023 120,960", gaps=[60, 200, 8, 60], spans="0-0.54,0.73-1")
        self.assertEqual(doc.blocks[0].text.split()[1:], ["120,960", "26.09.2023"])

    def test_a_token_that_is_not_a_number_shape_is_not_used(self) -> None:
        doc = self.segment(["הסעות", "2022", ">"], "Tf 2022")
        self.assertEqual(doc.blocks[0].text, "הסעות 2022 >")

    def test_a_currency_sign_ends_a_segment(self) -> None:
        doc = self.segment(["סכום", "20000", "₪"], "256,000 ₪")
        self.assertEqual(doc.blocks[0].text, "סכום 20000 ₪")  # one word: left to its own reading (FAKE_WORD empty)

    def test_a_currency_sign_is_not_taken_into_a_run(self) -> None:
        doc = self.read(["סכום", "20000", "₪"], "256,000")
        self.assertEqual(doc.blocks[0].text, "סכום 256,000 ₪")

    def test_no_second_reading_when_number_checks_are_off(self) -> None:
        doc = self.read(["1.", "תאגיד"], "1.1.", check_numbers=False)
        self.assertEqual(doc.blocks[0].text, "1. תאגיד")
        self.assertEqual(self.second_readings(), [])


if __name__ == "__main__":
    unittest.main()
