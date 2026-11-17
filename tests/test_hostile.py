"""D5d: hostile and broken input, gathered in one place.

Every probe from the D4 reviews, and a few more, must end **in bounded time** with **a valid
record** that says what was not read. Nothing hangs, crashes or loses text silently. Each probe
asserts its time under ``LIMIT`` seconds (generous, for slow CI runners) and validates its record.
Limits are patched small where a real-size input would only slow the suite.
"""

import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import heocr
from heocr import api, htmlread
from heocr.docx import reader as docx_reader
from tests.synth import html as H
from tests.synth.docx import Docx, para, table
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf
from tests.synth.pptx import Deck, Slide, group, shape
from tests.synth.pptx import para as slide_para
from tests.synth.pptx import table as slide_table
from tests.synth.xlsx import Sheet, Workbook, c, row

LIMIT = 30.0
FAKE_SOFFICE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_soffice.py")]


class HostileInputTest(unittest.TestCase):
    def read(self, body: bytes, **kwargs) -> heocr.Document:
        start = time.monotonic()
        doc = heocr.extract(body, **kwargs)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, LIMIT, f"took {elapsed:.1f} s")
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        doc.markdown  # the views must be bounded too
        doc.text
        return doc

    def reasons(self, doc) -> list[str]:
        return [u.reason for u in doc.unread]

    # -- nesting -------------------------------------------------------------------
    def test_html_nesting_far_beyond_the_cap(self) -> None:
        for name, body in {
            "divs": "<div>" * 20000 + "<p>עמוק</p>",
            "tables": "<table><tr><td>" * 3000 + "עמוק",
            "lists": "<ul><li>" * 5000 + "עמוק",
        }.items():
            with self.subTest(name):
                doc = self.read(H.page(body + "<p>אחרי</p>"))
                self.assertIn("עמוק", doc.text)
                self.assertIn("html_nesting_flattened", doc.flags)

    def test_pptx_groups_nested_far_beyond_the_cap(self) -> None:
        nested = shape([slide_para("עמוק")], (0, 0, 100, 40))
        for _ in range(5000):
            nested = group([nested], (300, 100, 300, 50), (0, 0, 300, 50))
        doc = self.read(Deck(slides=[Slide([nested, shape([slide_para("אחרי")], (50, 300, 300, 40))])]).build())
        self.assertIn("עמוק", doc.text)
        self.assertIn("group_depth_flattened", doc.flags)

    # -- spans and merges -----------------------------------------------------------
    def test_huge_spans_are_capped_everywhere(self) -> None:
        cases = {
            "html": H.page('<table><tr><td colspan="2000000000" rowspan="2000000000">רחב</td><td>א</td></tr></table>'),
            "docx": Docx(body=[table([[("רחב", {"span": 2000000000}), "א"], ["ב", "ג"]])]).build(),
            "pptx": Deck(slides=[Slide([slide_table([[("רחב", {"gridSpan": 2000000000, "rowSpan": 2000000000}), "א"], ["ב", "ג"]])])]).build(),
        }
        for name, body in cases.items():
            with self.subTest(name):
                doc = self.read(body)
                cell = next(b for b in doc.blocks if b.kind == "table").cells[0][0]
                self.assertLessEqual(cell.colspan, heocr.contract.MAX_SPAN)
                self.assertIn("span_capped", cell.flags)
                self.assertLess(len(doc.markdown), 50_000)

    def test_xlsx_hostile_merges_and_stray_cells(self) -> None:
        rows = [row(r, [c(f"A{r}", r), c(f"B{r}", r)]) for r in range(1, 1001)]
        full = self.read(Workbook(sheets=[Sheet("א", rows=rows[:1], merges=["A1:XFD1048576"] * 2000)]).build())
        self.assertEqual(len(next(b for b in full.blocks if b.kind == "table").cells), 1)
        stray = self.read(Workbook(sheets=[Sheet("א", rows=[row(1, [c("A1", 1), c("B1", 1), c("XFD1", inline="x")])] + rows[1:] + [row(1048576, [c("A1048576", 9)])])]).build())
        self.assertEqual(len(next(b for b in stray.blocks if b.kind == "table").cells), 1001)

    # -- size limits ------------------------------------------------------------------
    def test_a_zip_bomb_office_file_is_refused_not_expanded(self) -> None:
        with mock.patch.object(docx_reader, "MAX_EXPANDED_BYTES", 10_000):
            doc = self.read(Docx(body=[para("א" * 20_000)]).build())
        self.assertEqual(self.reasons(doc), ["docx_unreadable"])
        self.assertIn("not_read", doc.flags)

    def test_too_many_zip_members(self) -> None:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'/>")
            for n in range(60):
                z.writestr(f"junk/{n}", "")
        with mock.patch.object(docx_reader, "MAX_MEMBERS", 50):
            doc = self.read(out.getvalue())
        self.assertEqual(self.reasons(doc), ["docx_unreadable"])

    def test_an_input_over_the_size_limit_is_refused_before_parsing(self) -> None:
        with mock.patch.object(api, "MAX_FILE_BYTES", 1000):
            with self.assertRaises(ValueError):
                heocr.extract(b"%PDF-" + b"0" * 2000)

    def test_html_over_its_character_and_element_limits(self) -> None:
        with mock.patch.object(htmlread, "MAX_HTML_CHARS", 1000):
            doc = self.read(H.page("<p>" + "א" * 2000 + "</p>"))
        self.assertEqual(self.reasons(doc), ["html_too_large"])
        with mock.patch.object(htmlread, "MAX_ELEMENTS", 50):
            doc = self.read(H.page("".join(f"<p>פסקה {n}</p>" for n in range(500))))
        self.assertEqual(self.reasons(doc), ["html_truncated"])

    def test_page_and_frame_limits(self) -> None:
        pdf = make_pdf([PdfPage(lines=lines_block([f"עמוד {n}"])) for n in range(6)])
        doc = self.read(pdf, max_pages=2, ocr=False)
        self.assertEqual(self.reasons(doc), ["page_limit"])
        self.assertEqual(len(doc.pages), 2)
        slides = Deck(slides=[Slide([shape([slide_para(f"שקף {n}")], (50, 50, 300, 40))]) for n in range(6)]).build()
        self.assertEqual(self.reasons(self.read(slides, max_pages=2)), ["page_limit"])

    def test_an_image_bomb_is_unreadable_not_allocated(self) -> None:
        from PIL import Image

        image = Image.new("1", (1, 1))
        out = io.BytesIO()
        image.save(out, format="PNG")
        body = bytearray(out.getvalue())
        body[16:24] = (200_000).to_bytes(4, "big") * 2  # IHDR: 200,000 x 200,000 pixels claimed
        doc = self.read(bytes(body))
        self.assertEqual(self.reasons(doc), ["image_unreadable"])

    # -- external programs ---------------------------------------------------------
    def test_a_converter_that_never_returns_is_stopped(self) -> None:
        from heocr import convert
        from tests.synth.cfb import make_cfb

        convert._IDENTITIES.clear()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FAKE_SOFFICE_HANG": "1", "FAKE_SOFFICE_PIDFILE": str(Path(tmp) / "pid")}):
            doc = self.read(make_cfb(["WordDocument", "1Table"]), soffice=FAKE_SOFFICE, convert_timeout=2)
        self.assertEqual(self.reasons(doc), ["conversion_timeout"])

    # -- broken and odd files -------------------------------------------------------
    def test_broken_and_truncated_files_end_with_a_record(self) -> None:
        docx = Docx(body=[para("שלום")]).build()
        xlsx_body = Workbook(sheets=[Sheet("א", rows=[row(1, [c("A1", 1)])])]).build()
        pptx_body = Deck(slides=[Slide([shape([slide_para("א")], (50, 50, 300, 40))])]).build()
        pdf = make_pdf([PdfPage(lines=lines_block(["שלום עולם"]))])
        cases = {
            "random bytes": os.urandom(4096),
            "empty": b"",
            "truncated pdf": pdf[: len(pdf) // 2],
            "truncated docx": docx[: len(docx) // 2],
            "truncated xlsx": xlsx_body[: len(xlsx_body) // 2],
            "truncated pptx": pptx_body[: len(pptx_body) // 2],
            "pdf header only": b"%PDF-1.7\n",
            "html of nothing but tags": b"<html>" + b"<b>" * 10000,
            "nul bytes": b"\x00" * 10000,
        }
        for name, body in cases.items():
            with self.subTest(name):
                doc = self.read(body, ocr=False)
                if not doc.blocks:
                    self.assertTrue(doc.unread or "no_text" in doc.flags or doc.format in ("empty", "unknown"), doc.flags)

    def test_text_outside_the_page_is_recorded(self) -> None:
        doc = self.read(make_pdf([PdfPage(lines=lines_block(["שלום עולם"]) + [TextLine("מחוץ לעמוד", -100, 700)])]), ocr=False)
        self.assertIn("text_outside_page", self.reasons(doc))
        self.assertNotIn("מחוץ", doc.text)


if __name__ == "__main__":
    unittest.main()
