"""D4c: a page that is two pages side by side is read as two, in the document's order."""

import unittest

import heocr
from tests.synth.pdf import PdfPage, TextLine, make_pdf

SPREAD = (1191.0, 842.0)  # two A4 pages side by side


def spread_page(right_texts, left_texts, *, rtl=True, extra=(), rules=()):
    lines = []
    for i, text in enumerate(right_texts):
        lines.append(TextLine(text, 1140, 780 - 18 * i) if rtl else TextLine(text, 650, 780 - 18 * i, rtl=False))
    for i, text in enumerate(left_texts):
        lines.append(TextLine(text, 545, 780 - 18 * i) if rtl else TextLine(text, 55, 780 - 18 * i, rtl=False))
    return PdfPage(lines=lines + list(extra), width=SPREAD[0], height=SPREAD[1], rules=list(rules))


RIGHT = ["עמוד ימני שורה ראשונה", "עמוד ימני שורה שנייה", "עמוד ימני שורה שלישית"]
LEFT = ["עמוד שמאלי שורה ראשונה", "עמוד שמאלי שורה שנייה", "עמוד שמאלי שורה שלישית"]


class SpreadTest(unittest.TestCase):
    def test_an_rtl_spread_reads_its_right_page_first(self) -> None:
        doc = heocr.extract(make_pdf([spread_page(RIGHT, LEFT)]), ocr=False)
        page = doc.pages[0]
        self.assertIn("spread_split", page.flags)
        self.assertIn("possible_spread", page.flags)
        self.assertNotIn("reading_order_unresolved_columns", page.flags)
        self.assertEqual(doc.text.splitlines(), RIGHT + LEFT)
        self.assertEqual([b.half for b in doc.blocks], ["right", "left"])
        gutter = page.signals["spread_gutter"]
        self.assertTrue(545 <= gutter[0] < SPREAD[0] / 2 < gutter[1] < 1140)  # the fold lies in the gutter
        self.assertEqual(page.coverage["chars_read"], page.coverage["chars_total"])
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        self.assertEqual(doc.to_dict()["blocks"][0]["half"], "right")

    def test_an_ltr_spread_reads_its_left_page_first(self) -> None:
        right = ["Right page first line", "Right page second line"]
        left = ["Left page first line", "Left page second line"]
        doc = heocr.extract(make_pdf([spread_page(right, left, rtl=False)]), ocr=False)
        self.assertEqual(doc.text.splitlines(), left + right)
        self.assertEqual([b.half for b in doc.blocks], ["left", "right"])

    def test_text_across_the_middle_is_not_split(self) -> None:
        across = [TextLine("כותרת רחבה שחוצה את אמצע העמוד מצד לצד ומכסה את השוליים שבאמצע", 760, 820, size=14)]
        doc = heocr.extract(make_pdf([spread_page(RIGHT, LEFT, extra=across)]), ocr=False)
        page = doc.pages[0]
        self.assertNotIn("spread_split", page.flags)
        self.assertIn("possible_spread", page.flags)
        self.assertTrue(all(b.half is None for b in doc.blocks))

    def test_one_empty_half_is_not_split(self) -> None:
        doc = heocr.extract(make_pdf([spread_page(RIGHT, [])]), ocr=False)
        self.assertNotIn("spread_split", doc.pages[0].flags)

    def test_a_table_across_the_middle_is_not_split(self) -> None:
        xs, ys = [300.0, 600.0, 900.0], [700.0, 670.0, 640.0]
        rules = [(xs[0], y, xs[-1], y) for y in ys] + [(x, ys[0], x, ys[-1]) for x in xs]
        cells = [TextLine("תא", x - 10, y - 20) for x in xs[1:] for y in ys[:-1]]
        doc = heocr.extract(make_pdf([spread_page(RIGHT, LEFT, extra=cells, rules=rules)]), ocr=False)
        self.assertNotIn("spread_split", doc.pages[0].flags)

    def test_a_single_page_is_never_split(self) -> None:
        lines = [TextLine("ימין", 545, 780), TextLine("שמאל", 150, 780)]
        doc = heocr.extract(make_pdf([PdfPage(lines=lines)]), ocr=False)
        self.assertNotIn("spread_split", doc.pages[0].flags)
        self.assertNotIn("possible_spread", doc.pages[0].flags)


if __name__ == "__main__":
    unittest.main()
