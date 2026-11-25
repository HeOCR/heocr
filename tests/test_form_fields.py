import unittest

import heocr
from heocr.contract import Block, Cell, Document, Line
from heocr.pdf.form_fields import mark
from tests.synth.pdf import PdfPage, TextLine, make_pdf
from tests.test_pdf_tables import table_page


def associations(doc):
    return [(item["label"], item["value"]) for block in doc.blocks
            for item in (block.checks or {}).get("form_fields", [])]


class FormFieldsTest(unittest.TestCase):
    def test_number_and_prose_values_keep_their_exact_header_bound_label(self):
        data = make_pdf([table_page([
            ["ערך", "סוג", "שדה"],
            ["481", "דגם", "קוד ניסוי"],
            ["כחול בהיר", "צבע", "מדגם בדיקה"],
        ])])
        original = heocr.extract(data, ocr=False)
        enriched = heocr.extract(data, ocr=False, form_fields=True)
        self.assertEqual(associations(original), [])
        self.assertEqual(associations(enriched), [("קוד ניסוי", "481"), ("מדגם בדיקה", "כחול בהיר")])
        self.assertEqual(enriched.text, original.text)
        self.assertEqual(enriched.tables_csv(), original.tables_csv())
        self.assertEqual(heocr.validate(enriched.to_dict()), [])

    def test_pairing_requires_same_row_and_document_scope(self):
        values = [
            Line("קוד:", [200, 80, 232, 93], "rtl"),
            Line("TK-481", [110, 80, 160, 93], "ltr"),
            Line("מצב: ניסיוני", [140, 114, 240, 127], "rtl"),
            Line("תאריך:", [200, 160, 240, 173], "rtl"),
            Line("2000-04-05", [120, 180, 166, 192], "ltr"),
        ]
        doc = Document("b"*64, 0, "pdf", "fixture", blocks=[
            Block(kind="text", text=line.text, page=0, bbox=line.bbox, lines=[line]) for line in values
        ])
        mark(doc)
        self.assertEqual(associations(doc), [("קוד", "TK-481"), ("מצב", "ניסיוני")])

    def test_unheaded_repeated_status_column_requires_consistent_label(self):
        doc = Document("c"*64,0,"pdf","fixture",blocks=[
            Block(kind="table",cells=[[Cell(field),Cell(value)] for field,value in [
                ("בדקו שכל הדלתות נסגרות", "כן"), ("בדקו שהכפתורים אינם חסומים", "תקין"),
                ("בדקו שכל המנורות פועלות", "חלקי"), ("משאבה", "כן"),
            ]])
        ])
        mark(doc)
        self.assertEqual(len(associations(doc)),3)

    def test_a_lone_colon_in_prose_does_not_create_a_field(self):
        data = make_pdf([PdfPage(lines=[TextLine("הסבר: זה חלק מן המאמר", 520, 700)])])
        self.assertEqual(associations(heocr.extract(data, ocr=False, form_fields=True)), [])
