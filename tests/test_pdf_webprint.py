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


def print_page(n: int, total: int, *, counter: str | None = None, stamp: bool = True, header_title: bool = True, menu=MENU, long_menu: bool = False, url: bool = True, time: bool = True) -> PdfPage:
    lines = []
    if header_title:
        lines.append(TextLine(TITLE, 430, 822, 8))
    if stamp:
        lines.append(TextLine("10:23 ,16.11.2026" if time else "16.11.2026", 30, 822, 8, rtl=False))
        if url:
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

    def test_a_date_and_time_without_a_url_is_evidence(self) -> None:
        doc = extract([print_page(n, 3, url=False) for n in range(1, 4)])
        self.assertIn("browser_print", doc.flags)
        self.assertTrue(nav(doc))

    def test_a_word_letterhead_with_a_counter_and_a_date_only_stays_in_the_body(self) -> None:
        letterhead = [[("משרד הרווחה והביטחון החברתי", 545)], [("רחוב הדוגמה 10 ירושלים", 545)], [("לכבוד", 545)], [("המציעים במכרז", 545)]]
        pages = [print_page(n, 3, url=False, time=False, header_title=False, menu=letterhead) for n in range(1, 4)]
        doc = extract(pages)
        self.assertNotIn("browser_print", doc.flags)
        self.assertEqual(nav(doc), [])
        body = "\n".join(b.text for b in doc.blocks if b.role == "body" and b.page == 0)
        for (text, _), in letterhead:
            self.assertIn(text, body)

    def test_a_long_line_above_the_title_keeps_the_page_as_it_is(self) -> None:
        doc = extract([print_page(n, 3, long_menu=True) for n in range(1, 4)])
        self.assertIn("browser_print", doc.flags)
        self.assertEqual(nav(doc), [])

    def test_too_few_pieces_are_not_a_menu(self) -> None:
        doc = extract([print_page(n, 3, menu=[[("דף הבית", 545)]]) for n in range(1, 4)])
        self.assertEqual(nav(doc), [])

    def last_page_with(self, rows):
        pages = [print_page(n, 3) for n in range(1, 4)]
        y = 400
        for row in rows:
            pages[-1].lines += [TextLine(text, x, y, 9) for text, x in row]
            y -= 18
        return extract(pages)

    def test_the_last_page_footer_menu_is_navigation(self) -> None:
        footer = [
            [("מידע שימושי", 545), ("תמיכה", 420), ("משרדים", 300)],  # "משרדים" is also in the first page's menu
            [("לכל השירותים", 545), ("התקשרו למוקד", 420), ("אודות האתר", 300)],  # and "שירותים"
            [("RSS", 545), ("צ'אט", 420), ("הצהרת נגישות", 300)],
            [("תנאי שימוש", 545), ("מפת האתר", 300)],
        ]
        doc = self.last_page_with(footer)
        moved = [b for b in doc.blocks if "site_footer_menu" in b.flags]
        self.assertTrue(moved)
        self.assertTrue(all(b.role == "navigation" and b.page == 2 for b in moved))
        navigation = "\n".join(b.text if b.kind != "table" else " ".join(c.text for r in b.cells for c in r) for b in moved)
        for row in footer:
            for text, _ in row:
                self.assertIn(text, navigation)
        self.assertIn("site_footer_menu", doc.pages[2].flags)
        body = "\n".join(b.text for b in doc.blocks if b.role == "body" and b.page == 2)
        self.assertIn(BODY[0], body)  # the page's own text stays in the body
        self.assertNotIn("מפת האתר", body)

    def test_a_signature_at_the_end_is_not_a_footer_menu(self) -> None:
        signature = [[("בברכה,", 545)], [("ישראל ישראלי", 545)], [("מנהל הרכש", 545)], [("משרד הדוגמה", 545)]]
        doc = self.last_page_with(signature)
        self.assertEqual([b for b in doc.blocks if "site_footer_menu" in b.flags], [])
        self.assertIn("מנהל הרכש", "\n".join(b.text for b in doc.blocks if b.role == "body"))

    def test_signatories_side_by_side_are_not_a_footer_menu(self) -> None:
        # Common at the end of a government decision: two signatories, side by side, row by row.
        signatories = [
            [("שר האוצר", 545), ("שר הרווחה", 300)],
            [("ישראל ישראלי", 545), ("משה כהן", 300)],
            [("חתימה", 545), ("חתימה", 300)],
        ]
        doc = self.last_page_with(signatories)
        self.assertEqual([b for b in doc.blocks if "site_footer_menu" in b.flags], [])
        self.assertNotIn("site_footer_menu", doc.pages[2].flags)
        body = "\n".join(b.text if b.kind != "table" else " ".join(c.text for r in b.cells for c in r) for b in doc.blocks if b.role == "body")
        self.assertIn("משה כהן", body)

    def test_a_footer_with_a_phone_number_is_site_chrome(self) -> None:
        footer = [[("צרו קשר", 545), ("מוקד *6050", 300)], [("נגישות", 545), ("פרטיות", 300)], [("תנאים", 545), ("עזרה", 300)]]
        doc = self.last_page_with(footer)
        self.assertTrue([b for b in doc.blocks if "site_footer_menu" in b.flags])

    def test_counter_rule(self) -> None:
        self.assertTrue(webprint._COUNTER.fullmatch("2/8"))
        self.assertTrue(webprint._COUNTER.fullmatch(" 12 / 30 "))
        self.assertFalse(webprint._COUNTER.fullmatch("5/2026 מכרז"))


if __name__ == "__main__":
    unittest.main()
