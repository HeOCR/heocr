import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import heocr
from heocr.ocr.tesseract import OcrLine
from heocr.pdf import fontmap
from heocr.pdf.lines import Atom
from tests.synth.fake_tesseract import NOTHING
from tests.synth.pdf import PdfPage, lines_block, make_pdf

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]
CODE = ""  # the 2016 David subset's code for ם


def page_atoms(words: list[str], font: str = "SynthDavid") -> list[Atom]:
    """Atoms of one RTL line, in visual order, for words given in logical order."""
    atoms, x, index = [], 500.0, 0
    for word in words:
        for ch in word:  # logical order, drawn right to left
            x -= 6
            atoms.append(Atom(ch, x, 100, x + 6, 112, 12, index, None, font if ch == CODE else ""))
            index += 1
        x -= 3
        atoms.append(Atom(" ", x, 100, x + 3, 112, 12, index))
        index += 1
    return atoms


class Box:
    """A stand-in image whose crop returns its box, so the stub OCR can answer per word."""

    def crop(self, box):
        return box


def run(words: list[str], answer=None, font_letters=None):
    layer = [w.replace("ם", CODE) for w in words]
    atoms = page_atoms(layer)
    truth = {}
    for word in fontmap.visual_words(atoms):
        box = (int((min(a.x0 for a in word) - fontmap.CROP_PAD_PT) * 4), int((min(a.y0 for a in word) - fontmap.CROP_PAD_PT) * 4))
        truth[box] = fontmap.logical_letters(word).replace(CODE, "ם")

    def recognize(box):
        text = truth.get(box[:2], "")
        if answer is not None:
            text = answer(text)
        return [OcrLine(text, [0, 0, 1, 1], [95.0])] if text else []

    return fontmap.learn([(0, atoms)], lambda i: (Box(), 4.0), recognize, font_letters or {})


class LearnTest(unittest.TestCase):
    WORDS = ["השרים", "ביום", "שירותים", "מיום", "חברתיים", "לאזרחים"]

    def test_learns_a_final_letter_from_agreeing_words(self) -> None:
        (fmap,) = run(self.WORDS)
        self.assertEqual(fmap.letters, {CODE: "ם"})
        self.assertEqual(fmap.evidence[CODE]["accepted"], 6)
        record = fmap.record()
        self.assertEqual(record["map"], {"U+F01A": "ם"})
        self.assertEqual(record["rule"], "font_private_use_by_ocr")

    def test_too_few_words_stay_unresolved(self) -> None:
        (fmap,) = run(self.WORDS[:2])
        self.assertEqual(fmap.letters, {})
        self.assertIn("fewer than 3", fmap.unresolved[CODE])

    def test_disagreeing_readings_are_rejected(self) -> None:
        # OCR that contradicts a known letter is not accepted at all.
        (fmap,) = run(self.WORDS, answer=lambda true: "ז" + true[1:])
        self.assertEqual(fmap.letters, {})
        self.assertEqual(fmap.evidence[CODE]["accepted"], 0)

    def test_split_readings_fall_below_agreement(self) -> None:
        readings = iter(["ם", "ם", "ן", "ם", "ן", "ם"])
        (fmap,) = run(self.WORDS, answer=lambda true: true[:-1] + next(readings))
        self.assertIn("agreement", fmap.unresolved[CODE])

    def test_a_confusion_at_the_wrong_place_is_not_a_vote(self) -> None:
        # OCR reads the word-final code as ו in half the words: ו at every word end cannot be it.
        readings = iter(["ו", "ם", "ו", "ם", "ו", "ם"])
        (fmap,) = run(self.WORDS, answer=lambda true: true[:-1] + next(readings))
        self.assertEqual(fmap.letters, {CODE: "ם"})
        self.assertEqual((fmap.evidence[CODE]["accepted"], fmap.evidence[CODE]["ruled_out"]), (3, 3))

    def test_a_letter_the_font_draws_itself_is_not_a_vote(self) -> None:
        readings = iter(["ן", "ם", "ם", "ן", "ם", "ם"])
        (fmap,) = run(self.WORDS, answer=lambda true: true[:-1] + next(readings), font_letters={"SynthDavid": Counter({"ן": 30})})
        self.assertEqual(fmap.letters, {CODE: "ם"})
        self.assertEqual(fmap.evidence[CODE]["ruled_out"], 2)

    def test_too_few_votes_after_confusions_stay_unresolved(self) -> None:
        readings = iter(["ו", "ו", "ו", "ו", "ם", "ם"])
        (fmap,) = run(self.WORDS, answer=lambda true: true[:-1] + next(readings))
        self.assertEqual(fmap.letters, {})
        self.assertIn("fewer than 3", fmap.unresolved[CODE])

    def test_letter_present_in_regular_form_blocks_the_map(self) -> None:
        (fmap,) = run(self.WORDS, font_letters={"SynthDavid": Counter({"ם": 40})})
        self.assertIn("regular form", fmap.unresolved[CODE])

    def test_final_letter_must_sit_at_word_ends(self) -> None:
        self.assertIn("word end", fontmap._check(CODE, "ם", 5, 5, 10, 2, Counter()))
        self.assertIsNone(fontmap._check(CODE, "ם", 5, 5, 10, 10, Counter()))
        self.assertIn("regular letter", fontmap._check(CODE, "נ", 5, 5, 10, 9, Counter()))

    def test_align(self) -> None:
        self.assertEqual(fontmap.align("השרי" + CODE, "השרים.", {CODE}), {4: "ם"})
        self.assertIsNone(fontmap.align("יו" + CODE + "י", "לובי", {CODE}))


class PdfRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata), "FAKE_WORD": "שלום"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def test_shifted_font_is_restored_from_the_page(self) -> None:
        lines = ["שלום שלום שלום", "שלום שלום", "שלום שלום שלום שלום"]
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(lines), remap={"ם": CODE}, font_name="SynthDavid")]), tesseract=FAKE)
        self.assertEqual(doc.pages[0].page_class, "T1")
        self.assertEqual([l.text for b in doc.blocks for l in b.lines], lines)
        self.assertEqual(doc.to_dict()["font_maps"][0]["map"], {"U+F01A": "ם"})
        repair = [r for r in doc.blocks[0].repairs if r["rule"] == "font_private_use_by_ocr"]
        self.assertEqual(repair, [{"rule": "font_private_use_by_ocr", "from": "U+F01A", "to": "ם", "count": 9}])
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_too_little_evidence_falls_back_to_page_ocr(self) -> None:
        lines = ["שלום עולם נאה", "שלום ראשון"]  # ם occurs 3 times, only twice in a word OCR can confirm
        with mock.patch.dict(os.environ, {"FAKE_WORD": NOTHING}):
            doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(lines), remap={"ם": CODE}, font_name="SynthDavid")]), tesseract=FAKE)
        page = doc.pages[0]
        self.assertEqual((page.page_class, page.route), ("T3", "ocr"))
        self.assertIn("text_layer_ignored_broken_mapping", page.flags)
        self.assertIn("unresolved", doc.to_dict()["font_maps"][0])

    def test_repairs_off_learns_nothing(self) -> None:
        lines = ["שלום שלום שלום", "שלום שלום"]
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(lines), remap={"ם": CODE}, font_name="SynthDavid")]), tesseract=FAKE, repairs=False)
        self.assertEqual(doc.font_maps, [])
        self.assertEqual(doc.pages[0].route, "ocr")


if __name__ == "__main__":
    unittest.main()
