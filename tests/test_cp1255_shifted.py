"""The cp1255_shifted_font repair: fonts that carry Hebrew as its cp1255 byte (eval's D-bench set A,
cases pdf_t3_latin1 and pdf_t3_pua)."""

import unittest

import heocr
from tests.synth.pdf import PdfPage, lines_block, make_pdf

HEBREW = "אבגדהוזחטיךכלםמןנסעףפץצקרשת"
LINES = [
    "המציע יגיש את הצעתו עד המועד הקבוע במסמכי המכרז",
    "ולא יאוחר מן התאריך שנקבע לכך בהודעה לכל המציעים",
    "הצעה שתוגש לאחר המועד לא תידון ותוחזר למגישה",
    "על המציע לצרף את כל המסמכים הנדרשים בסעיף זה",
]


def shifted(kind: str) -> dict[str, str]:
    """A ToUnicode remap that writes every Hebrew letter as its cp1255 byte (Latin-1 or U+F000 + byte)."""
    return {ch: chr((0xF000 if kind == "pua" else 0) + ch.encode("cp1255")[0]) for ch in HEBREW}


def read(page, **kwargs):
    doc = heocr.extract(make_pdf([page]), ocr=False, **kwargs)
    assert heocr.validate(doc.to_dict()) == []
    return doc


class ShiftedFontTest(unittest.TestCase):
    def test_a_latin1_or_private_use_font_reads_as_hebrew(self) -> None:
        plain = read(PdfPage(lines=lines_block(LINES)))
        for kind in ("latin1", "pua"):
            with self.subTest(kind):
                doc = read(PdfPage(lines=lines_block(LINES), remap=shifted(kind)))
                self.assertEqual(doc.text, plain.text)
                self.assertIn("font_cp1255_shifted", doc.flags)
                self.assertIn("font_cp1255_shifted", doc.pages[0].flags)
                self.assertEqual(doc.pages[0].page_class, "T1")
                logged = [r for b in doc.blocks for r in b.repairs if r["rule"] == "cp1255_shifted_font"]
                self.assertEqual(sum(r["count"] for r in logged), sum(1 for line in LINES for ch in line if ch in HEBREW))
                code = "U+00F9" if kind == "latin1" else "U+F0F9"  # ש is cp1255 0xF9
                self.assertIn({"rule": "cp1255_shifted_font", "from": code, "to": "ש"}, [{k: r[k] for k in ("rule", "from", "to")} for r in logged])

    def test_repairs_off_keeps_the_codes(self) -> None:
        doc = read(PdfPage(lines=lines_block(LINES), remap=shifted("latin1")), repairs=False)
        self.assertNotIn("font_cp1255_shifted", doc.flags)
        self.assertIn("ù", doc.text)

    def test_latin_text_with_accents_is_not_shifted(self) -> None:
        french = ["Le soumissionnaire déposera son offre à la date prévue dans le dossier", "Une offre déposée après la date ne sera pas examinée ni ouverte", "Le candidat joindra tous les documents exigés à cet article précis"]
        doc = read(PdfPage(lines=lines_block(french, right=545)))
        self.assertNotIn("font_cp1255_shifted", doc.flags)
        self.assertIn("déposera", doc.text)

    def test_too_few_words_are_not_evidence(self) -> None:
        doc = read(PdfPage(lines=lines_block(["המציע יגיש"]), remap=shifted("latin1")))
        self.assertNotIn("font_cp1255_shifted", doc.flags)


if __name__ == "__main__":
    unittest.main()
