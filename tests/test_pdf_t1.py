import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import heocr
from heocr.cli import main
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf, visual_order

LINES = [
    "שלום עולם",
    "סעיף 3.1 לחוק המכרזים",
    "ב-2026 נקבע",
    "סכום 100 ₪",
    "50% מהסכום",
    "מיום 15.7.2020 ועד",
    "8,500,000 ₪",
    "Collision control הוא",
    "שָׁלוֹם לְךָ",
]


def lines_of(doc) -> list[str]:
    return [line.text for block in doc.blocks for line in (block.lines or [])]


class FixtureTest(unittest.TestCase):
    def test_fixture_layout_follows_bidi(self) -> None:
        self.assertEqual(visual_order("סעיף 3.1 ABC"), "ABC 3.1 ףיעס")
        self.assertEqual(visual_order("סכום 100 ₪"), "₪ 100 םוכס")
        self.assertEqual(visual_order("ב-2026"), "2026-ב")


class T1ReadingTest(unittest.TestCase):
    def test_exact_lines_in_every_stream_state(self) -> None:
        for order in ("logical", "visual"):
            for spaces in (True, False):
                with self.subTest(order=order, spaces=spaces):
                    page = PdfPage(lines=lines_block(LINES), stream_order=order, real_spaces=spaces)
                    doc = heocr.extract(make_pdf([page]))
                    self.assertEqual(lines_of(doc), LINES)
                    self.assertEqual(doc.pages[0].page_class, "T1")
                    self.assertEqual(doc.pages[0].coverage["status"], "read")

    def test_record_and_views(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(LINES[:2]))]))
        payload = doc.to_dict()
        self.assertEqual(heocr.validate(payload), [])
        self.assertEqual(payload["document"]["format"], "pdf")
        self.assertEqual(payload["document"]["page_count"], 1)
        self.assertIn("pypdfium2", payload["producer"]["components"])
        block = payload["blocks"][0]
        self.assertEqual(block["source"], "text_layer")
        self.assertEqual(block["page"], 0)
        first, second = block["lines"]
        self.assertLess(first["bbox"][1], second["bbox"][1])  # top-left origin
        self.assertEqual(doc.text, "שלום עולם\nסעיף 3.1 לחוק המכרזים\n")

    def test_ambiguous_latin_number_run_is_flagged(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(["סעיף 3.1 ABC"]))]))
        self.assertIn("bidi_ambiguous_ltr_run", doc.blocks[0].flags)

    def test_ltr_line(self) -> None:
        page = PdfPage(lines=[TextLine("Hello world 2026", 50, 780, rtl=False)])
        doc = heocr.extract(make_pdf([page]))
        self.assertEqual(lines_of(doc), ["Hello world 2026"])
        self.assertEqual(doc.blocks[0].direction, "ltr")

    def test_paragraphs_split_on_vertical_gap(self) -> None:
        lines = lines_block(["פסקה ראשונה", "המשך הפסקה"]) + lines_block(["פסקה שנייה"], top=700)
        doc = heocr.extract(make_pdf([PdfPage(lines=lines)]))
        self.assertEqual([b.text for b in doc.blocks], ["פסקה ראשונה\nהמשך הפסקה", "פסקה שנייה"])

    def test_clear_two_columns_read_column_major_and_flagged(self) -> None:
        right = lines_block(["עמודה ימנית אחת", "עמודה ימנית שתיים"], right=545)
        left = lines_block(["עמודה שמאלית אחת", "עמודה שמאלית שתיים"], right=250)
        doc = heocr.extract(make_pdf([PdfPage(lines=right + left)]))
        self.assertEqual(lines_of(doc), ["עמודה ימנית אחת", "עמודה ימנית שתיים", "עמודה שמאלית אחת", "עמודה שמאלית שתיים"])
        page = doc.pages[0]
        self.assertIn("reading_order_column_major_unverified", page.flags)
        self.assertFalse(page.coverage["selection_safe"])

    def test_unresolved_columns_are_flagged_not_guessed(self) -> None:
        lines = lines_block(["שורה רחבה אחת בלבד"]) + [TextLine("תא ימני", 545, 760), TextLine("תא שמאלי", 200, 760)]
        doc = heocr.extract(make_pdf([PdfPage(lines=lines)]))
        self.assertIn("reading_order_unresolved_columns", doc.pages[0].flags)
        self.assertEqual(lines_of(doc), ["שורה רחבה אחת בלבד", "תא ימני", "תא שמאלי"])

    def test_pages_and_empty_page(self) -> None:
        pdf = make_pdf([PdfPage(lines=lines_block(["עמוד ראשון"])), PdfPage(), PdfPage(lines=lines_block(["עמוד שלישי"]))])
        doc = heocr.extract(pdf)
        self.assertEqual([p.page_class for p in doc.pages], ["T1", "empty", "T1"])
        self.assertEqual([b.page for b in doc.blocks], [0, 2])
        self.assertEqual(doc.text, "עמוד ראשון\fעמוד שלישי\n")
        self.assertIn("<!-- page 3 -->", doc.markdown)

    def test_broken_pdf_is_reported(self) -> None:
        doc = heocr.extract(b"%PDF-1.7\n garbage")
        self.assertEqual(doc.unread[0].reason, "pdf_unreadable")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_cli_on_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            path.write_bytes(make_pdf([PdfPage(lines=lines_block(["שלום עולם"]))]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main([str(path), "-q"]), 0)
            self.assertEqual(out.getvalue(), "שלום עולם\n")


if __name__ == "__main__":
    unittest.main()
