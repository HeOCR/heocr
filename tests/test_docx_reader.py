import io
import unittest
import zipfile

import heocr
from tests.synth.docx import Docx, abstract_num, lvl, num, para, run, style, table

FIELD = (
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> PAGEREF _Toc1 \\h </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    "<w:r><w:t>7</w:t></w:r>"
    '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
)
WHITE = '<w:r><w:rPr><w:color w:val="FFFFFF"/></w:rPr><w:t>ריק</w:t></w:r>'
TEXTBOX = (
    "<w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><wp:anchor><a:graphic><a:graphicData>"
    "<wps:wsp><wps:txbx><w:txbxContent>" + para("תוכן תיבה") + "</w:txbxContent></wps:txbx></wps:wsp>"
    "</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
    "<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>" + para("תוכן תיבה") + "</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
    "</mc:AlternateContent></w:r>"
)
IMAGE = '<w:r><w:drawing><wp:inline><a:graphic><a:graphicData><a:blip r:embed="rId99"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'


def tender() -> bytes:
    d = Docx()
    d.styles = [
        style("Normal", "Normal"),
        style("Heading1", "heading 1", based_on="Normal", outline=0, num_id=1),
        style("Heading2", "heading 2", based_on="Normal", outline=1, num_id=1, ilvl=1),
        style("Appendix", "Appendix Title", based_on="Normal", outline=0),
        style("TOC1", "toc 1", based_on="Normal", outline=0),
        style("Secret", "Secret", kind="character", hidden=True),
        style("ListHeb", "List Hebrew", based_on="Normal", num_id=2),
    ]
    d.styles[0] = d.styles[0].replace('w:type="paragraph"', 'w:type="paragraph" w:default="1"')
    d.abstracts = [
        abstract_num(0, [lvl(0, pstyle="Heading1"), lvl(1, pstyle="Heading2"), lvl(2)]),
        abstract_num(1, [lvl(0, "hebrew1", "%1.")]),
    ]
    d.nums = [num(1, 0), num(2, 1)]
    d.body = [
        para("תוכן עניינים", FIELD, style="TOC1"),
        para("הגדרות", style="Heading1"),
        # The volunteer's case: a hidden paragraph whose text is a literal "2." (not a list item).
        para(run("2.", hidden=True)),
        para("המציע", style="Heading2"),
        para("סעיף משנה", num_id=1, ilvl=2),
        para("תנאי סף", style="Heading1"),
        para("ניסיון", style="Heading2"),
        para("פריט ראשון", style="ListHeb"),
        para("פריט שני", style="ListHeb"),
        para("טקסט ", run("סמוי", rstyle="Secret"), "גלוי", run(" מחוק", deleted=True)),
        para(WHITE),
        para("שורה", "\n", "שנייה"),
        para("לפני ", TEXTBOX, "אחרי"),
        para(IMAGE),
        table(
            [
                ["שם", ("סכום", {"span": 2})],
                [("ספק\n\nראשי", {"vmerge": "restart"}), "100 ₪", "שורה\nשבורה"],
                [("", {"vmerge": "continue"}), "200 ₪", "ג"],
            ]
        ),
        para("1.", outline=0),
        para("נספח א'", style="Appendix"),
    ]
    d.footnotes = {1: "הערת שוליים"}
    d.endnotes = {2: "הערת סיום"}
    d.comments = {0: "הערת סוקר"}
    d.headers = ["כותרת עליונה"]
    d.footers = ["עמוד"]
    return d.build()


class DocxReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = heocr.extract(tender())
        cls.payload = cls.doc.to_dict()
        cls.body = [b for b in cls.doc.blocks if b.role == "body"]

    def find(self, text: str):
        for block in self.doc.blocks:
            if block.text == text:
                return block
        self.fail(f"no block {text!r}")

    def test_format_and_record(self) -> None:
        self.assertEqual(self.payload["document"]["format"], "docx")
        self.assertEqual(heocr.validate(self.payload), [])
        self.assertIn("defusedxml", self.payload["producer"]["components"])

    def test_heading_numbers_from_styles(self) -> None:
        headings = [(b.label, b.text, b.level) for b in self.body if b.kind == "heading"]
        self.assertEqual(
            headings,
            [("1.", "הגדרות", 1), ("1.1.", "המציע", 2), ("2.", "תנאי סף", 1), ("2.1.", "ניסיון", 2), (None, "נספח א'", 1)],
        )

    def test_list_items(self) -> None:
        self.assertEqual(self.find("סעיף משנה").label, "1.1.1.")
        self.assertEqual(self.find("סעיף משנה").kind, "list_item")
        self.assertEqual([self.find("פריט ראשון").label, self.find("פריט שני").label], ["א.", "ב."])

    def test_toc_and_letterless_outline_are_not_headings(self) -> None:
        toc = self.find("תוכן עניינים7")
        self.assertEqual(toc.kind, "text")
        self.assertEqual(self.find("1.").kind, "text")

    def test_hidden_deleted_white_and_field_codes(self) -> None:
        mixed = self.find("טקסט גלוי")
        self.assertIn("hidden_text_dropped", mixed.flags)
        self.assertNotIn("ריק", self.doc.text)
        self.assertNotIn("PAGEREF", self.doc.text)
        self.assertNotIn("מחוק", self.doc.text)
        self.assertIn("hidden_paragraph_dropped", self.doc.flags)
        self.assertIn("white_paragraph_dropped", self.doc.flags)

    def test_line_break_and_text_box_once(self) -> None:
        self.assertEqual(self.find("שורה\nשנייה").direction, "rtl")
        self.assertEqual(self.find("לפני אחרי").kind, "text")
        boxes = [b for b in self.doc.blocks if b.text == "תוכן תיבה"]
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0].role, "textbox")

    def test_image_is_reported(self) -> None:
        self.assertEqual(sum(b.kind == "figure" for b in self.doc.blocks), 1)
        self.assertIn("image_not_read", [u.reason for u in self.doc.unread])

    def test_table(self) -> None:
        tables = [b for b in self.doc.blocks if b.kind == "table"]
        self.assertEqual(len(tables), 1)
        t = tables[0]
        self.assertEqual(t.direction, "rtl")
        cells = [[(c.text, c.colspan, c.rowspan) for c in row] for row in t.cells]
        self.assertEqual(
            cells,
            [
                [("שם", 1, 1), ("סכום", 2, 1)],
                [("ספק\nראשי", 1, 2), ("100 ₪", 1, 1), ("שורה\nשבורה", 1, 1)],
                [("", 1, 1), ("200 ₪", 1, 1), ("ג", 1, 1)],
            ],
        )
        self.assertIn("merged_above", t.cells[2][0].flags)
        self.assertIn("| שם | סכום |  |\n|---|---|---|\n| ספק<br>ראשי | 100 ₪ | שורה<br>שבורה |", self.doc.markdown)

    def test_notes_headers_footers_kept_apart(self) -> None:
        roles = {b.role: b for b in self.doc.blocks if b.role != "body"}
        self.assertEqual(roles["footnote"].text, "הערת שוליים")
        self.assertEqual(roles["footnote"].note_id, "1")
        self.assertEqual(roles["endnote"].note_id, "2")
        self.assertEqual(roles["comment"].text, "הערת סוקר")
        self.assertEqual(roles["header"].text, "כותרת עליונה")
        self.assertEqual(roles["footer"].text, "עמוד")
        text = self.doc.text
        body_end = text.index("--- Footnotes ---")
        for note in ("הערת שוליים", "הערת סיום", "כותרת עליונה"):
            self.assertGreater(text.index(note), body_end)

    def test_text_view_order(self) -> None:
        text = self.doc.text
        expected = ["1. הגדרות", "1.1. המציע", "1.1.1. סעיף משנה", "2. תנאי סף", "2.1. ניסיון", "א. פריט ראשון", "נספח א'"]
        positions = [text.index(item) for item in expected]
        self.assertEqual(positions, sorted(positions))


class DocxRobustnessTest(unittest.TestCase):
    def test_broken_document_xml_is_reported(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w:document")
        doc = heocr.extract(buffer.getvalue())
        self.assertEqual(doc.format, "docx")
        self.assertEqual(doc.unread[0].reason, "docx_unreadable")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_entity_expansion_is_refused(self) -> None:
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>'
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", bomb)
        doc = heocr.extract(buffer.getvalue())
        self.assertEqual(doc.unread[0].reason, "docx_unreadable")
        self.assertEqual(doc.blocks, [])

    def test_num_id_and_ilvl_inherit_separately(self) -> None:
        # Style "3" sets only ilvl 2 and is based on "2", which sets numId 9 and ilvl 1.
        d = Docx()
        d.styles = [
            style("1", "List 1", num_id=9, ilvl=0),
            style("2", "List 2", based_on="1", ilvl=1),
            style("3", "List 3", based_on="2", ilvl=2),
        ]
        d.abstracts = [abstract_num(0, [lvl(0), lvl(1), lvl(2)])]
        d.nums = [num(9, 0)]
        d.body = [
            para("א", style="1"),
            para("ב", style="1"),
            para("ג", style="2"),
            para("ד", style="2"),
            para("ה", style="3"),
            para("ו", style="3"),
            para("ז", style="3"),
        ]
        doc = heocr.extract(d.build())
        self.assertEqual([b.label for b in doc.blocks], ["1.", "2.", "2.1.", "2.2.", "2.2.1.", "2.2.2.", "2.2.3."])

    def test_hidden_numbered_paragraph_still_counts(self) -> None:
        d = Docx()
        d.abstracts = [abstract_num(0, [lvl(0), lvl(1)])]
        d.nums = [num(1, 0)]
        d.body = [
            para(run("1.", hidden=True), num_id=1, ilvl=0),
            para("ראשון", num_id=1, ilvl=1),
            para(run("2.", hidden=True), num_id=1, ilvl=0),
            para("שני", num_id=1, ilvl=1),
        ]
        doc = heocr.extract(d.build())
        self.assertEqual([(b.label, b.text) for b in doc.blocks], [("1.1.", "ראשון"), ("2.1.", "שני")])

    def test_numbered_section_break_paragraph_is_counted_and_flagged(self) -> None:
        d = Docx()
        d.abstracts = [abstract_num(0, [lvl(0)])]
        d.nums = [num(1, 0)]
        section_break = para(num_id=1, ilvl=0).replace("</w:pPr>", "<w:sectPr/></w:pPr>", 1)
        d.body = [para("א", num_id=1, ilvl=0), section_break, para("ב", num_id=1, ilvl=0)]
        doc = heocr.extract(d.build())
        self.assertEqual([b.label for b in doc.blocks], ["1.", "2.", "3."])
        self.assertIn("numbering_on_section_break_paragraph", doc.blocks[1].flags)

    def test_paragraph_ilvl_overrides_style_field_by_field(self) -> None:
        d = Docx()
        d.styles = [style("L", "List", num_id=9, ilvl=0)]
        d.abstracts = [abstract_num(0, [lvl(0), lvl(1)])]
        d.nums = [num(9, 0)]
        d.body = [para("א", style="L"), para("ב", style="L").replace("<w:pPr>", '<w:pPr><w:numPr><w:ilvl w:val="1"/></w:numPr>', 1)]
        doc = heocr.extract(d.build())
        self.assertEqual([b.label for b in doc.blocks], ["1.", "1.1."])

    def test_fixture_tables_have_a_grid_and_a_closing_paragraph(self) -> None:
        body = Docx(body=[table([["א", ("ב", {"span": 2})]])]).build()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml = archive.read("word/document.xml").decode()
        self.assertEqual(xml.count("<w:gridCol "), 3)
        self.assertTrue(xml.rstrip().endswith("</w:tbl><w:p><w:pPr><w:bidi/></w:pPr></w:p><w:sectPr></w:sectPr></w:body></w:document>"))
        self.assertEqual(heocr.extract(body).blocks[0].kind, "table")

    def test_white_text_on_a_shaded_paragraph_is_kept(self) -> None:
        shaded = para(WHITE).replace("<w:pPr>", '<w:pPr><w:shd w:val="clear" w:fill="1F3864"/>', 1)
        doc = heocr.extract(Docx(body=[shaded]).build())
        self.assertEqual(doc.text, "ריק\n")

    def test_minimal_document_without_styles_or_numbering(self) -> None:
        d = Docx(body=[para("שלום עולם")])
        doc = heocr.extract(d.build())
        self.assertEqual(doc.text, "שלום עולם\n")


if __name__ == "__main__":
    unittest.main()
