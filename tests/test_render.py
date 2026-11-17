import unittest

from heocr.contract import Block, Cell, Document, Unread


def _doc() -> Document:
    doc = Document(sha256="0" * 64, size=1, format="docx", format_evidence="test")
    doc.blocks = [
        Block(kind="heading", text="מבוא", level=1, label="1.", source="office_xml"),
        Block(kind="list_item", text="סעיף ראשון", label="1.1.", source="office_xml"),
        Block(kind="list_item", text="בלי רווח", label="(א)", source="office_xml", flags=["label_suffix_nothing"]),
        Block(kind="table", cells=[[Cell("שם"), Cell("סכום")], [Cell("א|ב"), Cell("שורה\nשנייה")]], source="office_xml"),
        Block(kind="text", text="הערה", role="footnote", note_id="1", source="office_xml"),
        Block(kind="text", text="כותרת עליונה", role="header", source="office_xml"),
    ]
    return doc


class RenderTest(unittest.TestCase):
    def test_text_keeps_body_first_and_sections_apart(self) -> None:
        text = _doc().text
        self.assertEqual(
            text,
            "1. מבוא\n1.1. סעיף ראשון\n(א)בלי רווח\nשם | סכום\nא|ב | שורה שנייה\n\n"
            "--- Footnotes ---\n[1] הערה\n\n--- Headers ---\nכותרת עליונה\n",
        )

    def test_markdown(self) -> None:
        md = _doc().markdown
        self.assertIn("# 1\\. מבוא", md)
        self.assertIn("1.1. סעיף ראשון", md)
        self.assertIn("| שם | סכום |\n|---|---|\n| א\\|ב | שורה<br>שנייה |", md)
        self.assertIn("[^1]: הערה", md)
        self.assertLess(md.index("סכום"), md.index("הערה"))

    def test_pages_and_unread_in_markdown(self) -> None:
        doc = Document(sha256="0" * 64, size=1, format="pdf", format_evidence="test", page_count=2)
        doc.blocks = [Block(kind="text", text="א", page=0), Block(kind="text", text="ב", page=1)]
        doc.unread = [Unread(scope="page", reason="class_T5_no_route", page=1)]
        self.assertEqual(doc.text, "א\fב\n")
        md = doc.markdown
        self.assertIn("<!-- page 2 -->", md)
        self.assertIn("<!-- heocr: unread page 2 (class_T5_no_route) -->", md)


class SpanClampTest(unittest.TestCase):
    def test_markdown_clamps_a_huge_colspan(self) -> None:
        from heocr.contract import MAX_SPAN, Block, Cell, Document

        doc = Document(sha256="0" * 64, size=1, format="docx", format_evidence="test")
        doc.blocks.append(Block(kind="table", cells=[[Cell("א", colspan=2_000_000_000)]], source="office_xml"))
        self.assertLess(len(doc.markdown), 10 * MAX_SPAN)


if __name__ == "__main__":
    unittest.main()
