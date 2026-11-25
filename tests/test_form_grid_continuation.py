"""Grid pairing needs corroborated geometry and page joining needs an explicit cue."""
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.contract import Block, Cell, Document, Line, Page
from heocr.ocr.tesseract import OcrResult, Tesseract
from tests.test_ocr import FAKE
from tests.test_ocr_forms import reading, word


class FormGridRecoveryTest(TestCase):
    def test_recovers_prefix_pairs_and_keeps_the_other_table(self):
        block = Block("table", page=0, source="ocr", direction="rtl", ocr={"status": "unverified"}, cells=[
            [Cell("מספר תיק", colspan=2), Cell("821", colspan=2)],
            [Cell("כתובת", colspan=2), Cell("", colspan=2)],
            [Cell("תיאור"), Cell("אורך"), Cell("גובה"), Cell("רוחב")],
            [Cell("דוגמה"), Cell("5"), Cell("6"), Cell("3")],
        ])
        doc = Document("a" * 64, 1, "pdf", "fixture", blocks=[
            Block("text", "טופס ניסוי", page=0),
            block,
            Block("text", "חתימה:", page=0),
        ])
        before = doc.to_dict()
        self.assertEqual([c["type"] for c in doc.to_forms()[0]["sections"][0]["children"]], ["table", "field"])
        doc._form_grid_recovery = True
        children = doc.to_forms()[0]["sections"][0]["children"]
        self.assertEqual([(c["label"], c["value"]) for c in children[:2]],
                         [("מספר תיק", "821"), ("כתובת", "")])
        retained = children[2]["table"]
        self.assertEqual((retained["rows"], retained["cols"], retained["cells"][0]["text"]), (2, 4, "תיאור"))
        self.assertEqual(doc.to_dict(), before)

    def test_two_wide_aligned_ocr_gaps_on_phone_feed_only_the_form_view(self):
        image = Image.new("RGB", (640, 768), "white")
        rows = [
            reading(word("טופס", (370, 70, 410, 91)), word("ניסוי", (321, 70, 360, 91))),
            reading(word("מספר", (530, 140, 569, 160)), word("תיק", (489, 140, 521, 160)),
                    word("421", (230, 140, 280, 160))),
            reading(word("כתובת", (515, 188, 569, 208)), word("הדוגמה", (214, 188, 280, 208))),
            reading(word("מיקוד", (480, 238, 525, 258)), word("4567", (91, 238, 137, 258))),
        ]
        draw = ImageDraw.Draw(image)
        for row in rows:
            for token in row.words:
                left, top, _, bottom = token.bbox
                draw.rectangle((left, top, left + 19, bottom), fill="black")
        photo = BytesIO()
        exif = Image.Exif()
        exif[272] = "Fictional phone"
        image.save(photo, "JPEG", dpi=(72, 72), exif=exif)

        def recognize(engine, source, dpi=72, psm=None):
            return OcrResult(rows) if source.size == image.size else OcrResult([])

        with TemporaryDirectory() as tmp:
            (Path(tmp) / "heb.traineddata").write_bytes(b"invented model")
            with (patch.dict(os.environ, {"FAKE_TESSDATA": tmp}),
                  patch.object(Tesseract, "recognize", recognize)):
                os.environ.pop("TESSDATA_PREFIX", None)
                options = dict(ocr_model="heb", check_numbers=False, tesseract=FAKE, form_fields=True)
                base = heocr.extract(photo.getvalue(), **options)
                candidate = heocr.extract(photo.getvalue(), form_grid_recovery=True, **options)
        self.assertEqual(base.to_dict(), candidate.to_dict())
        self.assertEqual(base.to_forms(), [])
        form, = candidate.to_forms()
        self.assertEqual([(n["label"], n["value"]) for n in form["sections"][0]["children"]],
                         [("מספר תיק", "421"), ("כתובת", "הדוגמה")])


class ContinuedFormTest(TestCase):
    def document(self, next_title, *, next_page=1, y=66, source="text_layer"):
        def line(text, page, y, *, role="body"):
            box = [300.0, y, 500.0, y + 16]
            ocr = {"status": "unverified"} if source == "ocr" else None
            return Block("text", text, page=page, bbox=box, source=source, role=role, ocr=ocr, lines=[Line(text, box)])
        return Document("b" * 64, 1, "pdf", "fixture", pages=[
            Page(i, 600, 800, "T5", "ocr") for i in range(next_page + 1)
        ], blocks=[
            line("טופס ניסוי", 0, 35, role="header"),
            line("פרטי המבקש", 0, 66),
            line("שם מלא: דוגמה", 0, 100),
            line("טופס ניסוי", 1, 35, role="header"),
            line(next_title, next_page, y),
            line("מספר תיק: 9", next_page, y + 32),
        ])

    def test_accepts_running_title_and_explicit_continuation(self):
        for heading, source in [("פרטי המבקש (המשך)", "text_layer"), ("פרטי המבקש (המשך", "ocr")]:
            with self.subTest(source=source):
                doc = self.document(heading, source=source)
                before = doc.to_dict()
                form, = doc.to_forms(join_pages=True)
                section, = form["sections"]
                self.assertEqual(section["title"], "פרטי המבקש")
                self.assertEqual(section["pages"], [1, 2])
                self.assertEqual([field["pages"] for field in section["children"]], [[1], [2]])
                self.assertEqual(doc.to_dict(), before)
                self.assertEqual(form["title"], "טופס ניסוי")
                self.assertEqual(doc.to_forms(), [])

    def test_abstains_for_unmarked_nonmatching_distant_and_midpage_headings(self):
        for next_title, page, y in [
            ("פרטי המבקש", 1, 75),
            ("פרטי הנכס (המשך)", 1, 75),
            ("פרטי המבקש (המשך)", 2, 75),
            ("פרטי המבקש (המשך)", 1, 390),
        ]:
            with self.subTest(next_title=next_title, page=page, y=y):
                doc = self.document(next_title, next_page=page, y=y)
                form, = doc.to_forms(join_pages=True)
                first, second = form["sections"]
                self.assertEqual((first["pages"], second["title"], second["pages"]), ([1], next_title, [page + 1]))
