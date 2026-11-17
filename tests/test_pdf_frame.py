"""The visible box of a PDF page: offset boxes read normally; text outside the box is unread."""

import unittest

import heocr
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

BODY = ["המציע יגיש את הצעתו עד המועד הקבוע", "במסמכי המכרז ולא יאוחר מן המועד"]
# A 3x3 ruled table, so rulings are placed by the same box as the text.
RULES = [(100, 500, 500, 500), (100, 540, 500, 540), (100, 580, 500, 580), (100, 620, 500, 620),
         (100, 500, 100, 620), (233, 500, 233, 620), (366, 500, 366, 620), (500, 500, 500, 620)]
CELLS = [TextLine(text, x, y) for y, row in ((605, ("שם", "תפקיד", "סכום")), (565, ("ספק א", "מנהל", "100")), (525, ("ספק ב", "עובד", "200")))
         for text, x in zip(row, (490, 357, 224))]


def page(**kwargs) -> PdfPage:
    return PdfPage(lines=lines_block(BODY, top=760) + CELLS, rules=list(RULES), **kwargs)


def read(*pages):
    doc = heocr.extract(make_pdf(list(pages)), ocr=False)
    assert heocr.validate(doc.to_dict()) == []
    return doc


def shape(doc):
    return [(b.kind, b.text if b.kind != "table" else [[c.text for c in r] for r in b.cells], [round(v, 1) for v in b.bbox or []]) for b in doc.blocks]


class OffsetBoxTest(unittest.TestCase):
    def test_a_page_whose_box_does_not_start_at_zero_reads_like_any_other(self) -> None:
        plain = read(page())
        for origin in ((-595.0, 0.0), (100.0, 50.0), (0.0, -842.0)):
            with self.subTest(origin=origin):
                moved = read(page(origin=origin))
                self.assertEqual(shape(moved), shape(plain))
                self.assertEqual(moved.unread, [])
                self.assertNotIn("text_outside_page", moved.pages[0].flags)
                self.assertEqual(len([b for b in moved.blocks if b.kind == "table"]), 1)


class OutsideTest(unittest.TestCase):
    def test_text_drawn_outside_the_box_is_unread_not_body(self) -> None:
        stray = TextLine("עמוד סמוך שנשאר בשכבה", 545 - 640, 700)  # wholly left of the page
        doc = read(PdfPage(lines=lines_block(BODY, top=760) + [stray]))
        self.assertNotIn("עמוד סמוך", doc.text)
        for line in BODY:
            self.assertIn(line, doc.text)
        (entry,) = [u for u in doc.unread if u.reason == "text_outside_page"]
        self.assertEqual(entry.page, 0)
        self.assertIn(f"{len(stray.text.replace(' ', ''))} characters", entry.detail)
        self.assertIn("text_outside_page", doc.pages[0].flags)

    def test_the_crop_box_is_what_a_viewer_shows(self) -> None:
        lines = [TextLine("שורה בחלק הגלוי", 545, 300), TextLine("שורה מחוץ לחיתוך", 545, 700)]
        doc = read(PdfPage(lines=lines, crop=(0, 0, 595, 421)))
        self.assertIn("שורה בחלק הגלוי", doc.text)
        self.assertNotIn("מחוץ לחיתוך", doc.text)
        self.assertEqual([u.reason for u in doc.unread], ["text_outside_page"])
        # Positions are inside the visible box: the top of the crop is y = 0.
        (block,) = doc.blocks
        self.assertLess(block.bbox[1], 421 - 300)

    def test_a_character_straddling_the_edge_stays_when_its_centre_is_inside(self) -> None:
        doc = read(PdfPage(lines=[TextLine("שלום", 22, 700)]))  # four 6-pt letters: the last one spans x -2..4
        self.assertEqual(doc.unread, [])
        self.assertIn("שלום", doc.text)


if __name__ == "__main__":
    unittest.main()
