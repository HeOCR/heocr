"""Offline checks of the glyph proof and its text-layer scope."""

import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageOps

import heocr
from heocr.pdf.punctuation import _match, _signature
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf


class GlyphEvidenceTest(unittest.TestCase):
    def test_round_and_square_templates_are_ordered_by_the_drawn_shape(self):
        for pair in ("()", "[]"):
            with self.subTest(pair=pair):
                mark = Image.new("L", (24, 36))
                draw = ImageDraw.Draw(mark)
                if pair == "()":
                    draw.line([(19, 2), (9, 9), (6, 18), (9, 27), (19, 33)], fill=255, width=3)
                else:
                    draw.line([(19, 2), (6, 2), (6, 33), (19, 33)], fill=255, width=3)
                opening, closing = _signature(mark), _signature(ImageOps.mirror(mark))
                self.assertEqual(_match(opening, pair, (opening, closing)), pair[1])
                self.assertEqual(_match(opening, pair, (closing, opening)), pair[1])
                self.assertEqual(_match(closing, pair, (closing, opening)), pair[0])
                self.assertIsNone(_match(opening, pair, (opening, opening)))

    def test_quote_recovers_the_font_code_with_no_mirroring(self):
        mark = Image.new("L", (24, 36))
        draw = ImageDraw.Draw(mark)
        for left in (2, 14):
            draw.line([(left + 6, 2), (left + 1, 13), (left + 1, 25)], width=4, fill=255)
        opening, closing = _signature(mark), _signature(ImageOps.flip(mark))
        self.assertEqual(_match(opening, "“”", (opening, closing)), "“")
        self.assertEqual(_match(closing, "“”", (opening, closing)), "”")
        self.assertIsNone(_match(opening, "“”", (opening, opening)))


class PdfPunctuationRouteTest(unittest.TestCase):
    @staticmethod
    def fixture():
        return make_pdf([PdfPage(lines=lines_block([
            "ראשון (מילה פתוחה",
            "שני ממשיך כאן) ואז (מילה נוספת).",
            "שלישי הערה ללא סוגריים",
            "רביעי )סימן מבודד; ליד סימן אחר( כאן",
        ]))])

    def test_position_witness_does_not_need_matching_endpoints_on_a_line(self):
        data = self.fixture()
        before = heocr.extract(data, ocr=False, check_numbers=False)
        expected = ["ראשון (מילה פתוחה", "שני ממשיך כאן) ואז (מילה נוספת).",
                    "שלישי הערה ללא סוגריים", "רביעי )סימן מבודד; ליד סימן אחר( כאן"]
        self.assertNotEqual(before.text.splitlines(), expected)
        planned = iter(ch for text in expected for ch in text if ch in "()")

        def proof(page, atoms):
            # The fake PDF has no embedded outline to sample. Supply the verdicts
            # of the glyph observer in the language's right-to-left order.
            for atom in sorted(atoms, key=lambda item: (round(item.y0), -item.x0)):
                atom.pdf_punctuation = next(planned)

        with patch("heocr.pdf.punctuation.observe", side_effect=proof) as observe:
            corrected = heocr.extract(data, ocr=False, check_numbers=False)
            self.assertEqual([line.text for block in corrected.blocks for line in block.lines or ()], expected)
            self.assertEqual(heocr.validate(corrected.to_dict()), [])
            self.assertIn("pdf_bidi_punctuation", [change["rule"] for block in corrected.blocks for change in block.repairs])
            before_boxes = [[line.bbox for line in block.lines or ()] for block in before.blocks]
            self.assertEqual([[line.bbox for line in block.lines or ()] for block in corrected.blocks], before_boxes)
            observe.assert_called_once()

    def test_unchanged_for_repairs_off_ltr_and_forced_ocr(self):
        ltr = make_pdf([PdfPage(lines=[TextLine('An English sentence (aside) “quote”.', 50, 780, rtl=False)])])
        with patch("heocr.pdf.punctuation.observe") as observe:
            doc = heocr.extract(ltr, ocr=False, check_numbers=False)
            self.assertIn("(aside)", doc.text)
            heocr.extract(self.fixture(), repairs=False, ocr=False, check_numbers=False)
            heocr.extract(self.fixture(), force_ocr=True, tesseract="/nonexistent", check_numbers=False)
            observe.assert_not_called()
