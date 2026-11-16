"""Opt-in checks against real external programs: LibreOffice and Tesseract.

LibreOffice renders a synthetic DOCX with real fonts. Its PDF must read back exactly, and so must a
scan of that PDF through Tesseract. CI does not run these (no external programs in CI). Run them
locally with ``HEOCR_TEST_SOFFICE=1 HEOCR_TEST_TESSERACT=1 python -m unittest tests.test_external_producer``.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import heocr
from tests.synth.docx import Docx, para, table

LINES = [
    "המציע יגיש את הצעתו עד המועד הקבוע",
    'סעיף 3.1 לחוק המכרזים, התשנ"ב-1992',
    'סכום ההצעה 8,500,000 ₪ כולל מע"מ',
    "מיום 15.7.2020 ועד ליום 31.12.2025",
    "נתונים נוספים ניתנים לעיון",
]


@unittest.skipUnless(os.environ.get("HEOCR_TEST_SOFFICE") and shutil.which("soffice"), "opt-in; needs LibreOffice")
class LibreOfficePdfTest(unittest.TestCase):
    def test_libreoffice_brackets_are_restored(self) -> None:
        bracketed = ["סעיף (א) קובע את התנאים", "המציע (להלן: הספק) יגיש", "על פי ההוראות (ראו נספח ב)"]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "brackets.docx"
            source.write_bytes(Docx(body=[para(t) for t in bracketed]).build())
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            doc = heocr.extract(Path(tmp) / "brackets.pdf", ocr=False)
        self.assertEqual([line.text for b in doc.blocks for line in b.lines], bracketed)

    def test_libreoffice_pdf_is_t1_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synth.docx"
            source.write_bytes(Docx(body=[para(t) for t in LINES]).build())
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            doc = heocr.extract(Path(tmp) / "synth.pdf")
        self.assertEqual(doc.pages[0].page_class, "T1", doc.pages[0].signals)
        self.assertEqual([line.text for b in doc.blocks for line in b.lines], LINES)


@unittest.skipUnless(os.environ.get("HEOCR_TEST_SOFFICE") and shutil.which("soffice"), "opt-in; needs LibreOffice")
class ConverterTest(unittest.TestCase):
    """D4a against the real LibreOffice: DOC, RTF and ODT made from a synthetic DOCX, read back."""

    FOOT = '<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="1"/></w:r>'

    def made_as(self, docx: bytes, fmt: str, tmp: str) -> bytes:
        source = Path(tmp) / "source.docx"
        source.write_bytes(docx)
        subprocess.run(["soffice", "--headless", "--convert-to", fmt, "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
        return (Path(tmp) / f"source.{fmt}").read_bytes()

    def test_doc_keeps_its_footnotes(self) -> None:
        # Eval: LibreOffice's text export drops footnotes. Through DOCX they survive.
        docx = Docx(body=[para("ראו את ההערה", self.FOOT, " בסוף")], footnotes={1: "הערת שוליים"}).build()
        with tempfile.TemporaryDirectory() as tmp:
            doc = heocr.extract(self.made_as(docx, "doc", tmp))
        self.assertEqual(doc.format, "doc")
        body = [b for b in doc.blocks if b.role == "body"]
        self.assertEqual(body[0].text, "ראו את ההערה בסוף")
        self.assertEqual(body[0].note_refs[0]["kind"], "footnote")
        self.assertIn("הערת שוליים", [b.text.strip() for b in doc.blocks if b.role == "footnote"])
        self.assertTrue(doc.components["libreoffice"].startswith("LibreOffice"))

    def test_a_body_ending_with_a_table_does_not_hang(self) -> None:
        # Eval: the text export hangs on this. The DOCX conversion must finish.
        docx = Docx(body=[para("לפני הטבלה"), table([["שם", "סכום"], ["ספק א", "100"]], borders=True)]).build()
        with tempfile.TemporaryDirectory() as tmp:
            body = self.made_as(docx, "doc", tmp)
        doc = heocr.extract(body, convert_timeout=120)
        self.assertEqual(doc.unread, [])
        (found,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in found.cells], [["שם", "סכום"], ["ספק א", "100"]])

    def test_rtf_and_odt(self) -> None:
        docx = Docx(body=[para(t) for t in LINES]).build()
        for fmt in ("rtf", "odt"):
            with self.subTest(fmt), tempfile.TemporaryDirectory() as tmp:
                doc = heocr.extract(self.made_as(docx, fmt, tmp))
                self.assertEqual(doc.format, fmt)
                self.assertEqual([b.text for b in doc.blocks if b.role == "body"], LINES)


@unittest.skipUnless(
    os.environ.get("HEOCR_TEST_TESSERACT") and shutil.which("tesseract") and shutil.which("soffice"),
    "opt-in; needs Tesseract with heb, and LibreOffice",
)
class TesseractScanTest(unittest.TestCase):
    def test_scan_of_a_libreoffice_pdf_is_t5_and_read_by_ocr(self) -> None:
        import pypdfium2 as pdfium

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synth.docx"
            source.write_bytes(Docx(body=[para(t) for t in LINES]).build())
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            pdf = pdfium.PdfDocument(str(Path(tmp) / "synth.pdf"))
            scan = Path(tmp) / "scan.pdf"
            pdf[0].render(scale=300 / 72).to_pil().convert("L").save(scan, resolution=300)
            pdf.close()
            doc = heocr.extract(scan)
        self.assertEqual(doc.pages[0].page_class, "T5")
        self.assertEqual(doc.pages[0].route, "ocr")
        self.assertEqual([line.text for b in doc.blocks for line in b.lines], LINES)
        self.assertTrue(doc.components["tesseract:heb.traineddata"].startswith("sha256:"))

    def test_image_files_png_and_multipage_tiff(self) -> None:
        import io

        import pypdfium2 as pdfium

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synth.docx"
            source.write_bytes(Docx(body=[para(t) for t in LINES]).build())
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            pdf = pdfium.PdfDocument(str(Path(tmp) / "synth.pdf"))
            picture = pdf[0].render(scale=300 / 72).to_pil().convert("L")
            pdf.close()
        png, tiff = io.BytesIO(), io.BytesIO()
        picture.save(png, format="PNG", dpi=(300, 300))
        picture.save(tiff, format="TIFF", save_all=True, append_images=[picture], dpi=(300, 300))
        doc = heocr.extract(png.getvalue())
        self.assertEqual((doc.format, doc.pages[0].page_class), ("png", "T5"))
        self.assertEqual([line.text for b in doc.blocks for line in b.lines], LINES)
        doc = heocr.extract(tiff.getvalue())
        self.assertEqual(len(doc.pages), 2)
        self.assertEqual([line.text for b in doc.blocks if b.page == 1 for line in b.lines], LINES)

    def test_scan_of_a_ruled_table_is_read_cell_by_cell(self) -> None:
        import pypdfium2 as pdfium

        rows = [["שם", "תפקיד", "סכום"], ["ספק ראשון", "מנהל", "100"], ["ספק שני", "עובד", "200"], ["ספק שלישי\n\nירושלים", "יועץ", "חיפה"]]  # words only: stock heb misread an isolated "300" as "100"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "table.docx"
            source.write_bytes(Docx(body=[para("טבלת הספקים"), table(rows, borders=True)]).build())
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            pdf = pdfium.PdfDocument(str(Path(tmp) / "table.pdf"))
            scan = Path(tmp) / "scan.pdf"
            pdf[0].render(scale=300 / 72).to_pil().convert("L").save(scan, resolution=300)
            pdf.close()
            doc = heocr.extract(scan)
        self.assertEqual(doc.pages[0].page_class, "T5")
        (found,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual(found.source, "ocr")
        self.assertEqual([[c.text for c in row] for row in found.cells], [[t.replace("\n\n", "\n") for t in row] for row in rows])
        self.assertEqual((found.ocr["cells_psm7"], found.ocr["cells_psm6"]), (11, 1))  # the two-line cell is a block
        self.assertIn("טבלת הספקים", doc.text)

    def test_amounts_in_a_scanned_table_are_read_by_the_number_model(self) -> None:
        # Stock heb read these cells as "12000", "00" and "1700"; eng reads them.
        import pypdfium2 as pdfium

        rows = [["שם", "סכום"], ["ספק ראשון", "300"], ["ספק שני", "8,500,000"], ["ספק שלישי", "15.7.2020"]]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "amounts.docx"
            source.write_bytes(Docx(body=[table(rows, borders=True)]).build())
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            pdf = pdfium.PdfDocument(str(Path(tmp) / "amounts.pdf"))
            scan = Path(tmp) / "scan.pdf"
            pdf[0].render(scale=300 / 72).to_pil().convert("L").save(scan, resolution=300)
            pdf.close()
            doc = heocr.extract(scan)
        (found,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in found.cells], rows)


if __name__ == "__main__":
    unittest.main()
