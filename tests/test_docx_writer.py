"""The DOCX writer: a ``Document`` written as a Word file, and read back by heocr's own DOCX reader.

All text here is invented for the tests.
"""

import contextlib
import io
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from defusedxml import ElementTree

import heocr
from heocr.cli import main
from heocr.contract import Block, Cell, Document
from heocr.docx.writer import parts

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _doc(**kwargs) -> Document:
    return Document(sha256="0" * 64, size=1, format=kwargs.pop("format", "pdf"), format_evidence="test", **kwargs)


def _sample() -> Document:
    """Two pages: a heading, mixed-script text, list items, a table with spans, and notes after the body."""
    doc = _doc(page_count=2)
    doc.blocks = [
        Block(kind="heading", text="דוח שנתי", level=1, label="1.", page=0, direction="rtl"),
        Block(kind="text", text="הסכום הוא 1,250 ש\"ח לפי סעיף QX-418.\nשורה שנייה\tאחרי טאב", page=0, direction="rtl"),
        Block(kind="list_item", text="פריט ראשון", label="1.1.", page=0, direction="rtl"),
        Block(kind="list_item", text="צמוד לתווית", label="(א)", page=0, direction="rtl", flags=["label_suffix_nothing"]),
        Block(kind="figure", page=1),
        Block(
            kind="table",
            page=1,
            direction="rtl",
            cells=[
                [Cell("שם", colspan=2), Cell("סכום", rowspan=2)],
                [Cell("א"), Cell("שורה\nשנייה"), Cell("", flags=["merged_above"])],
                [Cell("ב"), Cell("TR-905"), Cell("310")],
            ],
        ),
        Block(kind="heading", text="Appendix", level=8, page=1, direction="ltr"),
        Block(kind="text", text="הערת שוליים", role="footnote", note_id="1"),
        Block(kind="text", text="כותרת הדף", role="header"),
        Block(kind="text", text="עמוד", role="footer"),
    ]
    return doc


def _read_back(data: bytes) -> Document:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "back.docx"
        path.write_bytes(data)
        return heocr.extract(path, ocr=False, convert=False)


def _body_xml(doc: Document):
    return ElementTree.fromstring(parts(doc)["word/document.xml"].encode()).find(W + "body")


class RoundTripTest(unittest.TestCase):
    def test_reads_back_to_the_same_body_text(self) -> None:
        doc = _sample()
        back = _read_back(doc.to_docx())
        self.assertEqual(back.flags, [])
        self.assertEqual(back.unread, [])
        body, _, _ = doc.text.partition("\n\n--- ")
        # Word has no page separator in its text: the reader gives the page break as a line break.
        self.assertEqual(back.text, body.replace("\f", "\n") + "\nFootnotes\n[1] הערת שוליים\nHeaders\nכותרת הדף\nFooters\nעמוד\n")

    def test_headings_and_directions_read_back(self) -> None:
        back = _read_back(_sample().to_docx())
        headings = [(b.text, b.level, b.direction) for b in back.blocks if b.kind == "heading"]
        self.assertEqual(headings, [("1. דוח שנתי", 1, "rtl"), ("Appendix", 6, "ltr")])
        table = next(b for b in back.blocks if b.kind == "table")
        self.assertEqual(table.direction, "rtl")
        self.assertNotIn("table_ltr_layout", table.flags)
        self.assertEqual([[(c.text, c.colspan, c.rowspan) for c in row] for row in table.cells], [
            [("שם", 2, 1), ("סכום", 1, 2)],
            [("א", 1, 1), ("שורה\nשנייה", 1, 1), ("", 1, 1)],
            [("ב", 1, 1), ("TR-905", 1, 1), ("310", 1, 1)],
        ])

    def test_a_docx_read_written_and_read_again_keeps_its_text(self) -> None:
        from tests.synth.docx import Docx, para, style, table

        source = Docx(
            styles=[style("H1", "heading 1", outline=0)],
            body=[
                para("כותרת", style="H1"),
                para("פסקה ראשונה עם מילה Latin באמצע."),
                table([["א", ("ב", {"span": 2})], [("ג", {"vmerge": "restart"}), "ד", "ה"], [("", {"vmerge": "continue"}), "ו", "ז"]]),
                para("סוף"),
            ],
            footnotes={2: "הערה קטנה"},
        ).build()
        first = _read_back(source)
        second = _read_back(first.to_docx())
        body, _, _ = first.text.partition("\n\n--- ")
        self.assertTrue(second.text.startswith(body + "\nFootnotes\n"), second.text)
        self.assertIn("[2] הערה קטנה", second.text)
        self.assertEqual([b.kind for b in second.blocks if b.role == "body"][:4], ["heading", "text", "table", "text"])


class PackageTest(unittest.TestCase):
    def test_parts_are_well_formed_xml_and_typed(self) -> None:
        data = _sample().to_docx()
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            names = package.namelist()
            self.assertEqual(names[0], "[Content_Types].xml")
            for name in names:
                ElementTree.fromstring(package.read(name))
            types = package.read("[Content_Types].xml").decode()
            for name in names:
                if name.startswith("word/") and not name.endswith(".rels"):
                    self.assertIn(f'PartName="/{name}"', types)

    def test_the_same_record_gives_the_same_bytes(self) -> None:
        self.assertEqual(_sample().to_docx(), _sample().to_docx())

    def test_an_empty_document_is_still_a_word_file(self) -> None:
        back = _read_back(_doc().to_docx())
        self.assertEqual(back.text, "")
        self.assertEqual(back.unread, [])

    def test_characters_xml_cannot_carry_are_dropped(self) -> None:
        doc = _doc()
        doc.blocks = [Block(kind="text", text="א\x00ב\x0cג\r\nד")]
        self.assertEqual(_read_back(doc.to_docx()).text, "אבג\nד\n")


class LayoutTest(unittest.TestCase):
    def test_rtl_paragraphs_and_script_runs(self) -> None:
        body = _body_xml(_sample())
        para = body.findall(W + "p")[1]
        self.assertIsNotNone(para.find(f"{W}pPr/{W}bidi"))
        runs = [("".join(t.text for t in r.iter(W + "t")), r.find(f"{W}rPr/{W}rtl") is not None) for r in para.findall(W + "r")]
        self.assertIn(("QX-418", False), runs)
        self.assertTrue(runs[0][1])
        self.assertTrue(all(rtl for text, rtl in runs if re.search("[א-ת]", text)))

    def test_ltr_paragraph_has_no_bidi(self) -> None:
        doc = _doc()
        doc.blocks = [Block(kind="text", text="Plain English", direction="ltr")]
        para = _body_xml(doc).find(W + "p")
        self.assertEqual(para.find(f"{W}pPr/{W}bidi").get(W + "val"), "0")
        self.assertIsNone(para.find(f".//{W}rtl"))

    def test_a_full_stop_after_latin_ends_the_rtl_sentence(self) -> None:
        from heocr.docx.writer import _script_runs

        self.assertEqual(_script_runs("לפי סעיף QX-418.", True), [("לפי סעיף ", True), ("QX-418", False), (".", True)])
        self.assertEqual(_script_runs("קוד AB 12 ו-CD", True), [("קוד ", True), ("AB 12", False), (" ו-", True), ("CD", False)])
        self.assertEqual(_script_runs("Total 12.", False), [("Total 12.", False)])

    def test_hebrew_font_and_complex_script_size(self) -> None:
        styles = ElementTree.fromstring(parts(_sample())["word/styles.xml"].encode())
        rpr = styles.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr")
        self.assertEqual(rpr.find(W + "rFonts").get(W + "cs"), "Arial")
        self.assertEqual(rpr.find(W + "szCs").get(W + "val"), "24")
        names = {s.get(W + "styleId"): s.find(W + "name").get(W + "val") for s in styles.findall(W + "style")}
        for level in range(1, 7):
            self.assertEqual(names[f"Heading{level}"], f"heading {level}")

    def test_a_page_break_between_pages_only(self) -> None:
        body = _body_xml(_sample())
        breaks = [p for p in body.iter(W + "p") if p.find(f"{W}pPr/{W}pageBreakBefore") is not None]
        texts = ["".join(t.text for t in p.iter(W + "t")) for p in breaks]
        # Page 2 starts with a table, which takes its break from an empty paragraph; the sections start a page.
        self.assertEqual(texts, ["", "Footnotes"])

    def test_a_break_skipped_by_a_figure_waits_for_the_next_block(self) -> None:
        doc = _doc(page_count=3)
        doc.blocks = [Block(kind="text", text="א", page=0), Block(kind="figure", page=1), Block(kind="text", text="ב", page=2)]
        breaks = [p for p in _body_xml(doc).iter(W + "p") if p.find(f"{W}pPr/{W}pageBreakBefore") is not None]
        self.assertEqual(["".join(t.text for t in p.iter(W + "t")) for p in breaks], ["ב"])

    def test_rtl_table_with_spans(self) -> None:
        tbl = _body_xml(_sample()).find(W + "tbl")
        self.assertIsNotNone(tbl.find(f"{W}tblPr/{W}bidiVisual"))
        self.assertEqual(len(tbl.findall(f"{W}tblGrid/{W}gridCol")), 3)
        first, second, _ = tbl.findall(W + "tr")
        self.assertEqual(first.find(f"{W}tc/{W}tcPr/{W}gridSpan").get(W + "val"), "2")
        self.assertEqual(first.findall(W + "tc")[1].find(f"{W}tcPr/{W}vMerge").get(W + "val"), "restart")
        self.assertIsNone(second.findall(W + "tc")[2].find(f"{W}tcPr/{W}vMerge").get(W + "val"))

    def test_a_rowspan_with_no_cell_below_keeps_the_grid(self) -> None:
        # HTML and PDF records leave out the cells a rowspan covers.
        doc = _doc(format="html")
        doc.blocks = [Block(kind="table", direction="rtl", cells=[[Cell("א", rowspan=2), Cell("ב")], [Cell("ג")]])]
        rows = _body_xml(doc).find(W + "tbl").findall(W + "tr")
        self.assertEqual(len(rows[1].findall(W + "tc")), 2)
        self.assertIsNotNone(rows[1].find(f"{W}tc/{W}tcPr/{W}vMerge"))
        self.assertEqual("".join(t.text for t in rows[1].findall(W + "tc")[1].iter(W + "t")), "ג")

    def test_short_rows_are_filled_and_huge_spans_clamped(self) -> None:
        doc = _doc()
        doc.blocks = [Block(kind="table", cells=[[Cell("א"), Cell("ב"), Cell("ג")], [Cell("ד")], [Cell("ה", colspan=2_000_000_000)]])]
        xml = parts(doc)["word/document.xml"]
        self.assertLess(len(xml), 200_000)
        rows = _body_xml(doc).find(W + "tbl").findall(W + "tr")
        spans = [sum(int(tc.find(f"{W}tcPr/{W}gridSpan").get(W + "val")) if tc.find(f"{W}tcPr/{W}gridSpan") is not None else 1 for tc in row.findall(W + "tc")) for row in rows]
        self.assertEqual(len(set(spans)), 1)


GOLDEN_BODY = (
    '<w:p><w:pPr><w:pStyle w:val="Heading2"/><w:bidi/></w:pPr><w:r><w:rPr><w:rtl/></w:rPr><w:t xml:space="preserve">2. כותרת</w:t></w:r></w:p>'
    '<w:p><w:pPr><w:bidi/><w:ind w:start="360"/></w:pPr><w:r><w:rPr><w:rtl/></w:rPr><w:t xml:space="preserve">- פריט </w:t></w:r>'
    '<w:r><w:t xml:space="preserve">AB</w:t></w:r></w:p>'
    '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:bidiVisual/><w:tblW w:w="0" w:type="auto"/><w:tblLook w:val="04A0"/></w:tblPr>'
    '<w:tblGrid><w:gridCol w:w="4513"/><w:gridCol w:w="4513"/></w:tblGrid>'
    '<w:tr><w:tc><w:tcPr><w:tcW w:w="4513" w:type="dxa"/></w:tcPr><w:p><w:pPr><w:bidi/></w:pPr><w:r><w:rPr><w:rtl/></w:rPr><w:t xml:space="preserve">א</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:tcPr><w:tcW w:w="4513" w:type="dxa"/></w:tcPr><w:p><w:pPr><w:bidi/></w:pPr><w:r><w:rPr><w:rtl/></w:rPr><w:t xml:space="preserve">7</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
)


class GoldenTest(unittest.TestCase):
    def test_document_xml(self) -> None:
        doc = _doc(format="docx")
        doc.blocks = [
            Block(kind="heading", text="כותרת", level=2, label="2."),
            Block(kind="list_item", text="פריט AB", label="-"),
            Block(kind="table", direction="rtl", cells=[[Cell("א"), Cell("7")]]),
        ]
        xml = parts(doc)["word/document.xml"]
        body = xml[xml.index("<w:body>") + len("<w:body>") : xml.index("<w:sectPr>")]
        self.assertEqual(body, GOLDEN_BODY)
        self.assertIn("<w:bidi/></w:sectPr>", xml)


class ApiAndCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.input = self.dir / "in.docx"
        self.input.write_bytes(_sample().to_docx())

    def test_write_docx(self) -> None:
        out = self.dir / "out.docx"
        heocr.write_docx(_sample(), out)
        self.assertEqual(out.read_bytes(), _sample().to_docx())

    def test_cli_by_suffix_and_by_format(self) -> None:
        for args, out in (([], self.dir / "a.docx"), (["-f", "docx"], self.dir / "b.bin")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(self.input), "--no-ocr", "-o", str(out), *args]), 0)
            self.assertTrue(zipfile.is_zipfile(out))
            self.assertIn("דוח שנתי", _read_back(out.read_bytes()).text)

    def test_cli_docx_to_a_pipe_and_not_to_a_terminal(self) -> None:
        buffer = io.BytesIO()
        stdout = mock.Mock(buffer=buffer, isatty=lambda: False)
        with mock.patch("sys.stdout", stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.input), "--no-ocr", "-f", "docx"]), 0)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(buffer.getvalue())))
        stdout = mock.Mock(buffer=io.BytesIO(), isatty=lambda: True)
        with mock.patch("sys.stdout", stdout), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main([str(self.input), "--no-ocr", "-f", "docx"])


if __name__ == "__main__":
    unittest.main()
