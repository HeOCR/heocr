import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import heocr
from heocr.cli import main
from heocr.contract import Block, Cell, Document
from tests.synth.pdf import PdfPage, TextLine, make_pdf


def paragraph(text, *, kind="text", level=None, page=0):
    return Block(kind, text, page=page, direction="rtl", level=level)


class FormExportTest(unittest.TestCase):
    def test_group_children_follow_each_pair_column_in_logical_order(self):
        cells = [[Cell(value) for value in row] for row in [
            ["שם המבקש", "דוגמה", "מספר תיק", "42"],
            ["כתובת", "", "תאריך הגשה", "2026-07-18"],
        ]]
        doc = Document("7" * 64, 1, "pdf", "fixture", blocks=[
            paragraph("טופס ניסוי", kind="heading", level=1),
            paragraph("פרטי המבקש", kind="heading", level=2),
            Block("table", page=0, direction="rtl", cells=cells),
        ])
        before = doc.to_dict()
        form, = doc.to_forms()
        self.assertEqual(form["schema"], "hdoc_form.v1")
        self.assertEqual(form["title"], "טופס ניסוי")
        self.assertEqual(form["sections"][0]["title"], "פרטי המבקש")
        group, = form["sections"][0]["children"]
        self.assertEqual((group["type"], group["columns"]), ("group", 2))
        self.assertEqual([(f["label"], f["value"], f["kind"]) for f in group["children"]], [
            ("שם המבקש", "דוגמה", "text"), ("כתובת", "", "text"),
            ("מספר תיק", "42", "number"), ("תאריך הגשה", "2026-07-18", "date"),
        ])
        self.assertTrue(all(f["slot"] == "box" and f["pages"] == [1] for f in group["children"]))
        self.assertEqual(len({f["id"] for f in group["children"]}), 4)
        self.assertEqual(doc.to_dict(), before)

    def test_literal_checkmarks_choices_and_blank_signature(self):
        doc = Document("8" * 64, 1, "pdf", "fixture", blocks=[
            paragraph("טופס לדוגמה", kind="heading", level=1),
            paragraph("חלופות", kind="heading", level=2, page=0),
            paragraph("[X] אפשר לקבל הודעות", page=1),
            paragraph("מתאים לסימון [ ]", page=1),
            paragraph("אפשרות: ( ) אדום (X) ירוק", page=1),
            paragraph("בחירה: [ ] גבוה [ ] נמוך", page=1),
            paragraph("אישור: [X]", page=1),
            paragraph("תקלה: [X] אחד [X] שני", page=1),
            paragraph("חתימה:", page=1),
        ])
        form, = doc.to_forms()
        section, trailing = form["sections"]
        self.assertEqual(section["pages"], [1, 2])
        self.assertEqual([(f["kind"], f["value"]) for f in section["children"]], [
            ("checkbox", "checked"), ("checkbox", "unchecked"), ("choice", "ירוק"), ("choice", ""),
            ("checkbox", "checked"),
        ])
        self.assertEqual(trailing["title"], None)
        self.assertEqual([(f["kind"], f["value"]) for f in trailing["children"]], [("signature", "")])

    def test_ordinary_table_is_kept_as_a_table_inside_a_form(self):
        doc = Document("9" * 64, 1, "pdf", "fixture", blocks=[
            paragraph("טופס ניסוי"),
            paragraph("[X] מסמכים מצורפים"),
            Block("table", page=0, direction="rtl", cells=[[Cell("כותרת", colspan=2)],
                                                           [Cell("פריט"), Cell("40")]]),
        ])
        form, = doc.to_forms()
        children = form["sections"][0]["children"]
        table = children[1]["table"]
        self.assertEqual(table["schema"], "heocr_table.v1")
        self.assertEqual(table["cells"][0]["colspan"], 2)
        self.assertEqual(table["pages"], [1, 1])

    def test_cli_enables_existing_pairing_only_for_explicit_form_view(self):
        fixture = make_pdf([PdfPage(lines=[
            TextLine("קוד ניסוי: מוכן", 530, 690),
            TextLine("מדגם: כחול", 530, 672),
        ])])
        before = heocr.extract(fixture, ocr=False)
        self.assertEqual(before.to_forms(), [])
        with TemporaryDirectory() as temp:
            pdf = Path(temp) / "input.pdf"
            output = Path(temp) / "form.json"
            pdf.write_bytes(fixture)
            self.assertEqual(main([str(pdf), "--no-ocr", "-f", "forms", "-o", str(output)]), 0)
            form, = json.loads(output.read_bytes())
        self.assertEqual([(f["label"], f["value"]) for f in form["sections"][0]["children"]],
                         [("קוד ניסוי", "מוכן"), ("מדגם", "כחול")])
        self.assertEqual(heocr.extract(fixture, ocr=False, form_fields=True).text, before.text)
