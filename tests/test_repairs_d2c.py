import io
import unittest
import zipfile

import heocr
from heocr import repairs
from heocr.textflags import digit_groups_suspect, text_flags
from tests.synth.docx import Docx, para, table
from tests.synth.pdf import PdfPage, lines_block, make_pdf

HEBREW = [
    "המציע יגיש את הצעתו עד המועד",
    "הקבוע במסמכי המכרז ולא יאוחר",
    "נתונים נוספים ניתנים לעיון",
]
BRACKETED = [
    "סעיף (א) קובע את התנאים",
    "המציע (להלן: הספק) יגיש",
    "על פי ההוראות (ראו נספח ב)",
    "והכול כמפורט (בכפוף לדין)",
]


def lines_of(doc) -> list[str]:
    return [line.text for block in doc.blocks for line in (block.lines or [])]


def paragraphs(texts: list[str]) -> list:
    # One line per paragraph: wide vertical gaps.
    return [line for i, t in enumerate(texts) for line in lines_block([t], top=780 - 40 * i)]


class EthNunTest(unittest.TestCase):
    def test_restored_and_logged(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(HEBREW), remap={"נ": "ð"}, font_name="Broken")]), ocr=False)
        self.assertEqual(doc.pages[0].page_class, "T1")
        self.assertEqual(lines_of(doc), HEBREW)
        entry = doc.blocks[0].repairs[0]
        self.assertEqual((entry["rule"], entry["from"], entry["to"]), ("latin1_eth_nun", "U+00F0", "נ"))
        self.assertEqual(entry["count"], sum(t.count("נ") for t in HEBREW))

    def test_decided_per_font_not_per_file(self) -> None:
        broken = PdfPage(lines=lines_block(HEBREW), remap={"נ": "ð"}, font_name="Broken")
        healthy = PdfPage(lines=lines_block(["נתונים ðאמת כאן"]), font_name="Healthy")  # has נ: its ð stays
        doc = heocr.extract(make_pdf([broken, healthy]), ocr=False)
        self.assertEqual([b.lines[0].text for b in doc.blocks if b.page == 0][0], HEBREW[0])
        self.assertIn("ð", [b.text for b in doc.blocks if b.page == 1][0])

    def test_off_with_repairs_off(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(HEBREW), remap={"נ": "ð"})]), ocr=False, repairs=False)
        self.assertEqual(doc.blocks, [])  # the page stays T3 and unread
        self.assertEqual(doc.pages[0].page_class, "T3")


class BracketTest(unittest.TestCase):
    # The fixture draws each character at its display position without mirroring the glyph, which is
    # what a glyph-mapping producer's layer holds (Word's): read by geometry, brackets come out ")א(".
    # With the brackets swapped in its ToUnicode map, it is a producer that maps logical characters.

    def test_glyph_mapped_layer_is_restored_per_document(self) -> None:
        page = PdfPage(lines=paragraphs(BRACKETED))
        doc = heocr.extract(make_pdf([page]), ocr=False)
        self.assertEqual(lines_of(doc), BRACKETED)
        self.assertTrue(all("brackets_mirrored" in b.flags for b in doc.blocks))
        self.assertEqual(doc.blocks[0].repairs[0]["rule"], "rtl_bracket_order")
        off = heocr.extract(make_pdf([page]), ocr=False, repairs=False)
        self.assertEqual(lines_of(off)[0], "סעיף )א( קובע את התנאים")

    def test_a_paragraph_valid_as_read_keeps_its_brackets(self) -> None:
        # Glyph-mapped paragraphs, plus one from a logically mapped source in the same document.
        glyph = PdfPage(lines=paragraphs(BRACKETED))
        logical = PdfPage(lines=paragraphs(["צילום (תמונה עליונה) מתוך האוסף"]), remap={"(": ")", ")": "("})
        doc = heocr.extract(make_pdf([glyph, logical]), ocr=False)
        self.assertEqual(lines_of(doc), BRACKETED + ["צילום (תמונה עליונה) מתוך האוסף"])
        self.assertIn("brackets_kept_valid_as_read", doc.blocks[-1].flags)

    def test_a_paragraph_whose_labels_read_right_only_mirrored_is_mirrored(self) -> None:
        # set A synth-018: a paragraph closing a parenthesis from the one before ("2008)") and opening
        # one into the next ("69י8(ג)(") looks balanced as read ("2008(" ... ")"), but its section
        # labels, "(ג)", read right only mirrored. Balance as read is then a false pair: mirrored.
        text = "הודעה מיום 2008) לפי סעיף 69י8(ג) או (ד) ובהתאם לסעיף (5"
        glyph = PdfPage(lines=paragraphs(BRACKETED + [text]))
        doc = heocr.extract(make_pdf([glyph]), ocr=False)
        self.assertEqual(lines_of(doc)[-1], text)
        self.assertNotIn("brackets_kept_valid_as_read", doc.blocks[-1].flags)
        self.assertEqual((repairs.bracket_labels("סעיף (ג) או (ד)"), repairs.bracket_labels("סעיף )ג( או )ד(")), (2, 0))

    def test_logically_mapped_layer_is_left_alone(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(lines=paragraphs(BRACKETED), remap={"(": ")", ")": "("})]), ocr=False)
        self.assertEqual(lines_of(doc), BRACKETED)
        self.assertFalse(any(b.repairs for b in doc.blocks))
        self.assertTrue(all("rtl_brackets_unverified" in b.flags for b in doc.blocks))

    def test_evidence_threshold(self) -> None:
        self.assertFalse(repairs.brackets_mirrored_in([")א(", ")ב("]))  # fewer than 3
        self.assertTrue(repairs.brackets_mirrored_in([")א(", ")ב(", ")ג(", ")ה(", "(ד)"]))  # 4 > 3 x 1
        self.assertFalse(repairs.brackets_mirrored_in([")א(", ")ב(", ")ג(", "(ד)"]))  # 3 is not > 3 x 1


class DirectionMarksTest(unittest.TestCase):
    def test_removed_in_paragraphs_and_cells(self) -> None:
        d = Docx(body=[para("בסעיף ‏2.2 להלן"), table([["‎סכום", "100 ₪"]])])
        doc = heocr.extract(d.build())
        self.assertEqual(doc.blocks[0].text, "בסעיף 2.2 להלן")
        self.assertEqual(doc.blocks[0].repairs, [{"rule": "direction_marks", "from": "U+200F", "to": "", "count": 1}])
        self.assertEqual(doc.blocks[1].cells[0][0].text, "סכום")
        self.assertEqual(doc.blocks[1].repairs, [{"rule": "direction_marks", "from": "U+200E", "to": "", "count": 1}])
        off = heocr.extract(d.build(), repairs=False)
        self.assertEqual(off.blocks[0].text, "בסעיף ‏2.2 להלן")


class DigitGroupsTest(unittest.TestCase):
    def test_flag_only(self) -> None:
        cases = {"000,005,8": True, "8,500,000": False, "0,5": False, "2019,2020": False, "1,2,3": False, "3,000,00": True}
        for token, expected in cases.items():
            self.assertEqual(digit_groups_suspect(token), expected, token)
        self.assertEqual(text_flags("סכום 000,005,8 ₪"), ["digit_groups_suspect"])

    def test_pdf_block_is_flagged_and_unchanged(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(["הסכום הוא 000,005,8 ₪"]))]), ocr=False)
        self.assertEqual(lines_of(doc), ["הסכום הוא 000,005,8 ₪"])
        self.assertIn("digit_groups_suspect", doc.blocks[0].flags)


class RuleListTest(unittest.TestCase):
    def test_new_rules_are_documented(self) -> None:
        for rule_id in ("direction_marks", "latin1_eth_nun", "rtl_bracket_order"):
            rule = repairs.RULES[rule_id]
            self.assertTrue(rule.reason and rule.evidence and rule.formats)
            before, after = rule.example
            self.assertEqual(getattr(repairs, rule_id)(before)[0], after)


if __name__ == "__main__":
    unittest.main()
