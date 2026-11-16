"""D4g: a web page printed to PDF; the site menu on its first page gets the navigation role."""

import unittest

import heocr
from heocr.pdf import webprint
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

TITLE = "החלטת ממשלה לדוגמה בנושא שירותים חברתיים"
MENU = [
    [("דף הבית", 545), ("משרדים", 430), ("שירותים", 320), ("צור קשר", 200)],
    [("משרד ראש הממשלה", 545)],
    [("ראשי", 545), ("החלטות ממשלה", 430), ("2016", 300)],
]
BODY = [
    "הממשלה מחליטה לאמץ את המלצות הצוות שהוקם לבחינת אספקת השירותים",
    "ולהטיל על המשרדים הנוגעים בדבר ליישם אותן בתוך שנה מיום ההחלטה",
    "המשרדים ידווחו לממשלה על התקדמות היישום אחת לשישה חודשים לפחות",
    "הצוות יכלול נציגים של משרדי האוצר והרווחה ושל רשויות מקומיות",
    "ההחלטה תיכנס לתוקף ביום פרסומה ותחול על כל ההתקשרויות החדשות",
    "מיום קבלת החלטה זו ועד לסיום הפרויקט יפעל הצוות לפי תכנית עבודה",
]


def print_page(n: int, total: int, *, counter: str | None = None, stamp: bool = True, header_title: bool = True, menu=MENU, long_menu: bool = False) -> PdfPage:
    lines = []
    if header_title:
        lines.append(TextLine(TITLE, 430, 822, 8))
    if stamp:
        lines.append(TextLine("10:23 ,16.11.2026", 30, 822, 8, rtl=False))
        lines.append(TextLine("https://www.example.gov.il/he/pages/dec1", 30, 14, 8, rtl=False))
    lines.append(TextLine(counter if counter is not None else f"{n}/{total}", 560, 14, 8, rtl=False))
    if n == 1:
        y = 780
        for row in menu:
            for text, x in row:
                lines.append(TextLine(text, x, y, 10))
            y -= 30
        if long_menu:
            lines.append(TextLine("שורה ארוכה מאוד שאינה תפריט אלא תוכן של ממש בעמוד", 545, y, 10))
            y -= 30
        lines.append(TextLine(TITLE, 545, y - 20, 22))
        lines += lines_block(BODY, top=y - 80)
    else:
        lines += lines_block(BODY, top=700)
    return PdfPage(lines=lines)


def extract(pages):
    doc = heocr.extract(make_pdf(pages), ocr=False)
    assert heocr.validate(doc.to_dict()) == []
    return doc


def nav(doc):
    return [b for b in doc.blocks if b.role == "navigation"]


class WebPrintTest(unittest.TestCase):
    def test_the_first_page_menu_is_navigation(self) -> None:
        doc = extract([print_page(n, 3) for n in range(1, 4)])
        self.assertIn("browser_print", doc.flags)
        moved = " ".join(b.text for b in nav(doc))
        for text, _ in [piece for row in MENU for piece in row]:
            self.assertIn(text, moved)
        self.assertTrue(all("site_menu" in b.flags and b.page == 0 for b in nav(doc)))
        body = [b.text for b in doc.blocks if b.role == "body" and b.page == 0]
        self.assertEqual(body[0], TITLE)  # the content starts with the page's title
        self.assertNotIn("דף הבית", "\n".join(body))
        self.assertIn("site_menu", doc.pages[0].flags)
        self.assertEqual({b.role for b in doc.blocks if b.page == 1}, {"header", "footer", "body"})  # only the first page
        text = doc.text
        self.assertNotIn("דף הבית", text.partition("--- Headers ---")[0])
        self.assertIn("--- Navigation ---", text)

    def test_the_title_by_size_when_the_header_has_none(self) -> None:
        doc = extract([print_page(n, 3, header_title=False) for n in range(1, 4)])
        self.assertIn("browser_print", doc.flags)
        self.assertTrue(nav(doc))
        self.assertEqual([b.text for b in doc.blocks if b.role == "body" and b.page == 0][0], TITLE)

    def test_a_menu_read_as_a_table_is_navigation(self) -> None:
        grid = [[("דף הבית", 545), ("משרדים", 430), ("שירותים", 320)]] * 3
        doc = extract([print_page(n, 3, menu=grid, header_title=False) for n in range(1, 4)])
        tables = [b for b in doc.blocks if b.kind == "table" and b.page == 0]
        self.assertTrue(tables)
        self.assertEqual({b.role for b in tables}, {"navigation"})

    def test_without_browser_evidence_nothing_changes(self) -> None:
        cases = {
            "no counter": [print_page(n, 3, counter=f"עמוד {n}") for n in range(1, 4)],
            "counter, no URL or date": [print_page(n, 3, stamp=False) for n in range(1, 4)],
            "counter of another total": [print_page(n, 3, counter=f"{n}/9") for n in range(1, 4)],
        }
        for name, pages in cases.items():
            with self.subTest(name):
                doc = extract(pages)
                self.assertNotIn("browser_print", doc.flags)
                self.assertEqual(nav(doc), [])

    def test_a_long_line_above_the_title_keeps_the_page_as_it_is(self) -> None:
        doc = extract([print_page(n, 3, long_menu=True) for n in range(1, 4)])
        self.assertIn("browser_print", doc.flags)
        self.assertEqual(nav(doc), [])

    def test_too_few_pieces_are_not_a_menu(self) -> None:
        doc = extract([print_page(n, 3, menu=[[("דף הבית", 545)]]) for n in range(1, 4)])
        self.assertEqual(nav(doc), [])

    def test_counter_rule(self) -> None:
        self.assertTrue(webprint._COUNTER.fullmatch("2/8"))
        self.assertTrue(webprint._COUNTER.fullmatch(" 12 / 30 "))
        self.assertFalse(webprint._COUNTER.fullmatch("5/2026 מכרז"))


if __name__ == "__main__":
    unittest.main()
