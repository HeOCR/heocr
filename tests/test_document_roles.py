from collections import Counter
import unittest

import heocr
from heocr.contract import Block, Document, Line, Page
from heocr.pdf.document_roles import apply
from tests.synth.pdf import PdfPage, TextLine, make_pdf


class DocumentRolesTest(unittest.TestCase):
    def test_pdf_api_tags_furniture_and_keeps_page_text_in_order(self):
        lines = [
            TextLine("כותרת המדגם", 520, 806),
            TextLine("שורת תוכן ראשונה", 520, 720),
            TextLine("שורת תוכן נוספת", 520, 695),
            TextLine("1 הערה לדוגמה", 520, 65),
            TextLine("מהדורה ניסיונית", 520, 24),
        ]
        data = make_pdf([PdfPage(lines=lines)])
        baseline = heocr.extract(data, ocr=False)
        candidate = heocr.extract(data, ocr=False, document_roles=True)
        self.assertFalse(any(b.role != "body" for b in baseline.blocks))
        self.assertEqual([b.role for b in candidate.blocks], ["header", "body", "body", "footnote", "footer"])
        self.assertEqual(candidate.text, "\n".join(line.text for line in lines) + "\n")
        self.assertEqual(candidate.pages[0].flags[-1], "document_roles_ordered")
        self.assertEqual(heocr.validate(candidate.to_dict()), [])

    def test_splits_geometry_lines_without_losing_the_original_log(self):
        lines = [Line("Footer", [20, 480, 90, 489], "ltr"),
                 Line("Body first", [20, 80, 90, 91], "ltr"),
                 Line("Heading", [20, 18, 90, 27], "ltr"),
                 Line("Body next", [20, 120, 90, 131], "ltr"),
                 Line("1 A note", [20, 457, 90, 466], "ltr")]
        mixed = Block(kind="text", text="\n".join(line.text for line in lines), page=0, source="ocr",
                      ocr={"status": "unverified"}, bbox=[20, 18, 90, 489], lines=lines,
                      checks={"test": True}, repairs=[{"rule": "fixture"}])
        doc = Document("a"*64, 0, "png", "image", pages=[Page(0, 300, 500, "T5", "ocr")], blocks=[mixed])
        plain = doc.text
        apply(doc)
        self.assertEqual(doc.text, plain)
        self.assertEqual(Counter(line.text for b in doc.blocks for line in b.lines),
                         Counter(line.text for line in lines))
        self.assertEqual([b.role for b in doc.blocks], ["header", "body", "body", "footnote", "footer"])
        self.assertEqual([b.text for b in doc.blocks], ["Heading", "Body first", "Body next", "1 A note", "Footer"])
        body = next(b for b in doc.blocks if b.role == "body")
        self.assertEqual(body.checks, {"test": True})
        self.assertEqual(sum(len(b.repairs) for b in doc.blocks), 1)
        self.assertTrue(all(b.ocr["status"] == "unverified" for b in doc.blocks))

    def test_top_body_title_keeps_original_order(self):
        data = make_pdf([PdfPage(lines=[TextLine("כותרת צמודה", 520, 806),
                                         TextLine("טקסט סמוך", 520, 787),
                                         TextLine("פסקה שניה", 520, 760)])])
        baseline = heocr.extract(data, ocr=False)
        candidate = heocr.extract(data, ocr=False, document_roles=True)
        self.assertEqual(candidate.to_dict(), baseline.to_dict())


    def test_declared_phone_is_skipped_even_with_margin_like_boxes(self):
        lines = [Line("Heading", [10, 10, 60, 24]), Line("Body", [10, 90, 70, 102]),
                 Line("Body again", [10, 120, 80, 135])]
        block = Block(kind="text", text="\n".join(line.text for line in lines), page=0, source="ocr",
                      ocr={"status": "unverified"}, bbox=[10, 10, 80, 135], lines=lines)
        doc = Document("a"*64, 0, "png", "image", pages=[Page(0,300,500,"T5","ocr")],
                       blocks=[block], _ocr_settings={"capture_type":"pixel"})
        before = doc.to_dict()
        apply(doc)
        self.assertEqual(doc.to_dict(), before)
