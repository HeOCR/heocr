import unittest

import heocr
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

BODY = ["המציע יגיש את הצעתו עד המועד", "הקבוע במסמכי המכרז ולא יאוחר"]


def tender_page(n: int, header: bool = True) -> PdfPage:
    lines = lines_block(BODY, top=700) + [TextLine("המציע", 545, 500)]  # a heading that repeats in mid-page
    if header:
        lines += [TextLine("משרד האוצר מכרז 5/2026", 545, 815), TextLine(f"עמוד {n}", 150, 815), TextLine("כל הזכויות שמורות", 545, 30)]
    return PdfPage(lines=lines)


def extract(pages):
    return heocr.extract(make_pdf(pages), ocr=False)


class FurnitureTest(unittest.TestCase):
    def test_running_header_page_number_and_footer_get_roles(self) -> None:
        doc = extract([tender_page(n) for n in range(1, 5)])
        headers = [b.text for b in doc.blocks if b.role == "header"]
        footers = [b.text for b in doc.blocks if b.role == "footer"]
        self.assertEqual(sorted(set(headers)), sorted({"משרד האוצר מכרז 5/2026", *{f"עמוד {n}" for n in range(1, 5)}}))
        self.assertEqual(footers, ["כל הזכויות שמורות"] * 4)
        body = [b.text for b in doc.blocks if b.role == "body"]
        self.assertNotIn("משרד האוצר מכרז 5/2026", "\n".join(body))
        self.assertIn("המציע", body)  # the repeated mid-page heading stays in the body
        self.assertTrue(all("page_furniture" in b.flags for b in doc.blocks if b.role != "body"))
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_columns_from_furniture_only_are_cleared(self) -> None:
        doc = extract([tender_page(n) for n in range(1, 5)])
        for page in doc.pages:
            self.assertNotIn("reading_order_unresolved_columns", page.flags)
            self.assertIn("columns_were_page_furniture", page.flags)
            self.assertTrue(page.coverage["selection_safe"])

    def test_text_view_keeps_furniture_out_of_the_body(self) -> None:
        text = extract([tender_page(n) for n in range(1, 5)]).text
        body, _, rest = text.partition("--- Headers ---")
        self.assertNotIn("עמוד 2", body)
        self.assertIn("עמוד 2", rest)

    def test_needs_three_pages_or_a_majority(self) -> None:
        pages = [tender_page(1), tender_page(2)] + [tender_page(n, header=False) for n in range(3, 6)]
        doc = extract(pages)  # 2 of 5 pages: neither 3 nor a majority
        self.assertFalse([b for b in doc.blocks if b.role != "body"])
        two = extract([tender_page(1), tender_page(2)])  # 2 of 2: a majority
        self.assertTrue([b for b in two.blocks if b.role == "header"])
        one = extract([tender_page(1)])
        self.assertFalse([b for b in one.blocks if b.role != "body"])

    def test_a_furniture_block_keeps_its_repair_log(self) -> None:
        pages = []
        for n in range(1, 5):
            page = tender_page(n)
            page.lines.append(TextLine("\uf0b7 נספח", 545, 30 - 12))  # a footer line with a symbol-font bullet
            pages.append(page)
        doc = extract(pages)
        logged = [r for b in doc.blocks if b.role == "footer" for r in b.repairs if r["rule"] == "symbol_font_bullet"]
        self.assertEqual(sum(r["count"] for r in logged), 4)
        self.assertIn("• נספח", "\n".join(b.text for b in doc.blocks if b.role == "footer"))


if __name__ == "__main__":
    unittest.main()
