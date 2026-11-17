"""D4e: PPTX decks, slide by slide. Synthetic decks only."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import heocr
from heocr import pptx
from tests.synth.pptx import Deck, Slide, chart, group, para, picture, shape, smartart, table

FAKE_TESSERACT = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]


def extract(deck: Deck, **kwargs) -> heocr.Document:
    doc = heocr.extract(deck.build(), **kwargs)
    assert heocr.validate(doc.to_dict()) == [], heocr.validate(doc.to_dict())
    return doc


def body(doc, slide=None):
    return [b.text for b in doc.blocks if b.role == "body" and b.kind != "table" and (slide is None or b.slide == slide)]


class SlideTest(unittest.TestCase):
    def test_slides_in_order_with_their_numbers(self) -> None:
        deck = Deck(slides=[Slide([shape([para("ראשונה")], (50, 50, 300, 40))]), Slide([shape([para("שנייה")], (50, 50, 300, 40))])], order=[2, 1])
        doc = extract(deck)
        self.assertEqual(doc.format, "pptx")
        self.assertEqual([(b.text, b.slide) for b in doc.blocks], [("שנייה", 1), ("ראשונה", 2)])  # the slide list decides
        self.assertEqual({b.source for b in doc.blocks}, {"office_xml"})
        self.assertEqual({b.tool for b in doc.blocks}, {"heocr.pptx"})
        self.assertIn("\f", doc.text)
        self.assertIn("<!-- slide 2 -->", doc.markdown)

    def test_reading_order_rows_right_to_left_in_an_rtl_deck(self) -> None:
        shapes = [
            shape([para("שמאל למטה")], (50, 300, 200, 40)),
            shape([para("ימין למעלה")], (500, 100, 200, 40)),
            shape([para("שמאל למעלה")], (50, 110, 200, 40)),
            shape([para("ימין למטה")], (500, 290, 200, 60)),
            shape([para("כותרת השקף")], (50, 400, 600, 40), ph="title"),  # the title comes first, wherever it is
        ]
        doc = extract(Deck(slides=[Slide(shapes)]))
        self.assertEqual(body(doc), ["כותרת השקף", "ימין למעלה", "שמאל למעלה", "ימין למטה", "שמאל למטה"])
        self.assertEqual(doc.blocks[0].kind, "heading")
        self.assertIn("deck_rtl", doc.flags)

    def test_an_ltr_deck_reads_left_to_right(self) -> None:
        shapes = [shape([para("Right", rtl=False)], (500, 100, 200, 40)), shape([para("Left", rtl=False)], (50, 100, 200, 40))]
        doc = extract(Deck(slides=[Slide(shapes)]))
        self.assertEqual(body(doc), ["Left", "Right"])
        self.assertEqual([b.direction for b in doc.blocks], ["ltr", "ltr"])
        self.assertIn("deck_ltr", doc.flags)

    def test_placeholders_take_their_layout_position(self) -> None:
        layout = [shape([], (50, 300, 600, 100), ph="body", idx=1), shape([], (50, 50, 600, 60), ph="title")]
        shapes = [shape([para("נקודה ראשונה")], ph="body", idx=1), shape([para("טקסט עליון")], (50, 150, 600, 40))]
        doc = extract(Deck(slides=[Slide(shapes)], layout_shapes=layout))
        self.assertEqual(body(doc), ["טקסט עליון", "נקודה ראשונה"])

    def test_groups_are_ordered_inside_with_mapped_coordinates(self) -> None:
        inner = [shape([para("ב")], (0, 0, 100, 50)), shape([para("א")], (200, 0, 100, 50))]  # child space: א on the right
        shapes = [group(inner, (300, 100, 300, 50), (0, 0, 300, 50)), shape([para("ג")], (50, 100, 200, 50))]
        self.assertEqual(body(extract(Deck(slides=[Slide(shapes)]))), ["א", "ב", "ג"])

    def test_paragraphs_bullets_numbering_and_breaks(self) -> None:
        paras = [
            para("פתיח"),
            para("ראשון", autonum="arabicPeriod"), para("שני", autonum="arabicPeriod"),
            para("משנה", autonum="hebrew2Minus", lvl=1), para("שלישי", autonum="arabicPeriod"),
            para("נקודה", bullet="•"), para("שורה\nשנייה"), para("סמל", bullet=""),
            para("מ-3", autonum="arabicParenBoth", start=3),
        ]
        doc = extract(Deck(slides=[Slide([shape(paras, (50, 50, 600, 400))])]))
        self.assertEqual([(b.label, b.text) for b in doc.blocks], [
            (None, "פתיח"), ("1.", "ראשון"), ("2.", "שני"), ("א-", "משנה"), ("3.", "שלישי"),
            ("•", "נקודה"), (None, "שורה\nשנייה"), ("•", "סמל"), ("(3)", "מ-3"),
        ])
        self.assertIn("bullet_symbol_mapped", doc.blocks[7].flags)

    def test_footer_placeholders_are_footers(self) -> None:
        shapes = [shape([para("תוכן")], (50, 50, 600, 40)), shape([para("משרד האוצר")], (50, 500, 300, 20), ph="ftr"), shape([para("4", rtl=None)], (600, 500, 50, 20), ph="sldNum")]
        doc = extract(Deck(slides=[Slide(shapes)]))
        self.assertEqual([b.text for b in doc.blocks if b.role == "footer"], ["4", "משרד האוצר"])
        self.assertEqual(body(doc), ["תוכן"])


class TableTest(unittest.TestCase):
    def test_slide_tables_with_spans(self) -> None:
        rows = [
            [("כותרת", {"gridSpan": 2}), ("", {"hMerge": 1}), "סכום"],
            [("קבוצה", {"rowSpan": 2}), "א", "100"],
            [("", {"vMerge": 1}), "ב", "200"],
        ]
        doc = extract(Deck(slides=[Slide([table(rows)])]))
        (t,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual(t.slide, 1)
        self.assertEqual(t.direction, "rtl")
        self.assertEqual([[(c.text, c.colspan, c.rowspan, c.flags) for c in r] for r in t.cells], [
            [("כותרת", 2, 1, []), ("סכום", 1, 1, [])],
            [("קבוצה", 1, 2, []), ("א", 1, 1, []), ("100", 1, 1, [])],
            [("", 1, 1, ["merged_above"]), ("ב", 1, 1, []), ("200", 1, 1, [])],
        ])


class AroundTheSlideTest(unittest.TestCase):
    def test_speaker_notes_are_notes(self) -> None:
        deck = Deck(slides=[Slide([shape([para("שקף")], (50, 50, 300, 40))], notes=[para("להזכיר את המועד"), para("ולהדגיש את הסכום")])])
        doc = extract(deck)
        self.assertEqual([(b.text, b.slide) for b in doc.blocks if b.role == "notes"], [("להזכיר את המועד", 1), ("ולהדגיש את הסכום", 1)])
        body_text, _, rest = doc.text.partition("--- Speaker notes ---")
        self.assertNotIn("להזכיר", body_text)
        self.assertIn("להזכיר", rest)
        self.assertNotIn("7", rest)  # the notes page's slide number is not a note

    def test_hidden_slides_and_shapes_are_read_and_flagged(self) -> None:
        deck = Deck(slides=[Slide([shape([para("גלוי")], (50, 50, 300, 40)), shape([para("מוסתר")], (50, 150, 300, 40), hidden=True)]), Slide([shape([para("שקף מוסתר")], (50, 50, 300, 40))], hidden=True)])
        doc = extract(deck)
        flags = {b.text: b.flags for b in doc.blocks}
        self.assertEqual(flags["גלוי"], [])
        self.assertEqual(flags["מוסתר"], ["hidden_shape"])
        self.assertEqual(flags["שקף מוסתר"], ["hidden_slide"])
        self.assertIn("hidden_slides_read", doc.flags)

    def test_smartart_text_charts_and_missing_pictures(self) -> None:
        deck = Deck(slides=[Slide([smartart(), chart((400, 50, 200, 200)), picture("rIdX")], smartart=["שלב ראשון", "שלב שני"])])
        doc = extract(deck)
        self.assertEqual([b.text for b in doc.blocks if "from_smartart" in b.flags], ["שלב ראשון", "שלב שני"])
        self.assertEqual(sorted(u.reason for u in doc.unread), ["chart_not_read", "image_not_read"])

    def test_pictures_are_read_by_ocr(self) -> None:
        image = Image.new("L", (600, 200), 255)
        ImageDraw.Draw(image).rectangle([20, 20, 580, 60], fill=0)
        png = io.BytesIO()
        image.save(png, format="PNG", dpi=(200, 200))
        deck = Deck(slides=[Slide([shape([para("לפני")], (50, 50, 300, 40)), picture("rIdP", (50, 200, 300, 100))], images={"rIdP": png.getvalue()})])
        with tempfile.TemporaryDirectory() as tmp:
            tessdata = Path(tmp) / "tessdata"
            tessdata.mkdir()
            (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
            with mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata)}):
                os.environ.pop("TESSDATA_PREFIX", None)
                doc = extract(deck, tesseract=FAKE_TESSERACT)
        self.assertEqual(body(doc), ["לפני", "שלום עולם\nשורה", "פסקה"])
        self.assertEqual({b.slide for b in doc.blocks}, {1})
        self.assertEqual(doc.blocks[1].source, "ocr")

    def test_slide_limit_and_a_broken_deck(self) -> None:
        deck = Deck(slides=[Slide([shape([para(f"שקף {n}")], (50, 50, 300, 40))]) for n in range(1, 6)])
        doc = extract(deck, max_pages=2)
        self.assertEqual(body(doc), ["שקף 1", "שקף 2"])
        self.assertEqual([u.reason for u in doc.unread], ["page_limit"])
        broken = heocr.extract(Deck(slides=[Slide(["<p:sp>"])]).build())
        self.assertEqual([u.reason for u in broken.unread], ["pptx_unreadable"])

    def test_direction_marks_are_repaired(self) -> None:
        doc = extract(Deck(slides=[Slide([shape([para("בסעיף ‏2.2")], (50, 50, 300, 40))])]))
        self.assertEqual(doc.blocks[0].text, "בסעיף 2.2")
        self.assertEqual(doc.blocks[0].repairs, [{"rule": "direction_marks", "from": "U+200F", "to": "", "count": 1}])


class ContractTest(unittest.TestCase):
    def test_slide_must_be_a_positive_integer(self) -> None:
        doc = extract(Deck(slides=[Slide([shape([para("א")], (50, 50, 300, 40))])]))
        payload = doc.to_dict()
        payload["blocks"][0]["slide"] = 0
        self.assertTrue(any("slide" in e for e in heocr.validate(payload)))

    def test_reading_order_is_a_pure_function(self) -> None:
        items = [pptx._Item(None, (0, 0, 10, 10), "sp"), pptx._Item(None, (20, 2, 30, 12), "sp"), pptx._Item(None, None, "sp")]
        self.assertEqual([i.box for i in pptx.reading_order(items, rtl=True)], [(20, 2, 30, 12), (0, 0, 10, 10), None])


@unittest.skipUnless(os.environ.get("HEOCR_TEST_SOFFICE") and shutil.which("soffice"), "opt-in; needs LibreOffice")
class LibreOfficePptxTest(unittest.TestCase):
    """A synthetic deck saved again by real LibreOffice reads back the same."""

    def test_libreoffice_roundtrip(self) -> None:
        deck = Deck(rtl=True, slides=[
            Slide([shape([para("כותרת המצגת")], (50, 30, 600, 50), ph="title"), shape([para("נקודה ראשונה", bullet="•"), para("נקודה שנייה", bullet="•")], (50, 120, 600, 120))], notes=[para("הערת דובר")]),
            Slide([table([["שם", "סכום"], ["ספק", "100"]])]),
        ])
        expected = extract(deck)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in" / "deck.pptx"
            source.parent.mkdir()
            source.write_bytes(deck.build())
            subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--convert-to", "pptx", "--outdir", tmp, str(source)], check=True, capture_output=True, timeout=180)
            doc = heocr.extract(Path(tmp) / "deck.pptx")
        self.assertEqual(body(doc), body(expected))
        self.assertEqual([[c.text for c in r] for b in doc.blocks if b.kind == "table" for r in b.cells], [["שם", "סכום"], ["ספק", "100"]])
        self.assertEqual([b.text for b in doc.blocks if b.role == "notes"], ["הערת דובר"])


if __name__ == "__main__":
    unittest.main()
