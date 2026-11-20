"""The visible box of a PDF page: offset boxes read normally; text outside the box is unread."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

BODY = ["המציע יגיש את הצעתו עד המועד הקבוע", "במסמכי המכרז ולא יאוחר מן המועד"]
# A 3x3 ruled table, so rulings are placed by the same box as the text.
RULES = [(100, 500, 500, 500), (100, 540, 500, 540), (100, 580, 500, 580), (100, 620, 500, 620),
         (100, 500, 100, 620), (233, 500, 233, 620), (366, 500, 366, 620), (500, 500, 500, 620)]
CELLS = [TextLine(text, x, y) for y, row in ((605, ("שם", "תפקיד", "סכום")), (565, ("ספק א", "מנהל", "100")), (525, ("ספק ב", "עובד", "200")))
         for text, x in zip(row, (490, 357, 224))]


def page(**kwargs) -> PdfPage:
    return PdfPage(lines=lines_block(BODY, top=760) + CELLS, rules=list(RULES), **kwargs)


def read(*pages):
    doc = heocr.extract(make_pdf(list(pages)), ocr=False)
    assert heocr.validate(doc.to_dict()) == []
    return doc


def shape(doc):
    return [(b.kind, b.text if b.kind != "table" else [[c.text for c in r] for r in b.cells], [round(v, 1) for v in b.bbox or []]) for b in doc.blocks]


class OffsetBoxTest(unittest.TestCase):
    def test_a_page_whose_box_does_not_start_at_zero_reads_like_any_other(self) -> None:
        plain = read(page())
        for origin in ((-595.0, 0.0), (100.0, 50.0), (0.0, -842.0)):
            with self.subTest(origin=origin):
                moved = read(page(origin=origin))
                self.assertEqual(shape(moved), shape(plain))
                self.assertEqual(moved.unread, [])
                self.assertNotIn("text_outside_page", moved.pages[0].flags)
                self.assertEqual(len([b for b in moved.blocks if b.kind == "table"]), 1)


class OutsideTest(unittest.TestCase):
    def test_text_drawn_outside_the_box_is_unread_not_body(self) -> None:
        stray = TextLine("עמוד סמוך שנשאר בשכבה", 545 - 640, 700)  # wholly left of the page
        doc = read(PdfPage(lines=lines_block(BODY, top=760) + [stray]))
        self.assertNotIn("עמוד סמוך", doc.text)
        for line in BODY:
            self.assertIn(line, doc.text)
        (entry,) = [u for u in doc.unread if u.reason == "text_outside_page"]
        self.assertEqual(entry.page, 0)
        self.assertIn(f"{len(stray.text.replace(' ', ''))} characters", entry.detail)
        self.assertIn("text_outside_page", doc.pages[0].flags)

    def test_the_crop_box_is_what_a_viewer_shows(self) -> None:
        lines = [TextLine("שורה בחלק הגלוי", 545, 300), TextLine("שורה מחוץ לחיתוך", 545, 700)]
        doc = read(PdfPage(lines=lines, crop=(0, 0, 595, 421)))
        self.assertIn("שורה בחלק הגלוי", doc.text)
        self.assertNotIn("מחוץ לחיתוך", doc.text)
        self.assertEqual([u.reason for u in doc.unread], ["text_outside_page"])
        # Positions are inside the visible box: the top of the crop is y = 0.
        (block,) = doc.blocks
        self.assertLess(block.bbox[1], 421 - 300)

    def test_a_character_straddling_the_edge_stays_when_its_centre_is_inside(self) -> None:
        doc = read(PdfPage(lines=[TextLine("שלום", 22, 700)]))  # four 6-pt letters: the last one spans x -2..4
        self.assertEqual(doc.unread, [])
        self.assertIn("שלום", doc.text)


FAKE_TESSERACT = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]


class RotatedPageTest(unittest.TestCase):
    """/Rotate turns the shown page; a text layer's boxes stay in the unrotated box. Anything that
    places layer boxes on a rendered image (region crops, masks of layer characters, number and
    font checks) must render the page in that same, unrotated frame."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tessdata = Path(self.tmp.name) / "tessdata"
        tessdata.mkdir()
        (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def page(self, rotate: int, turned: bool = False) -> bytes:
        """A word of the text layer over an image region whose only ink lies exactly under the word."""
        from PIL import Image, ImageDraw

        word = TextLine("מכרזים", 300, 200, 20)  # its loose box: x 240..300, y 200..220 (user space)
        k = 4  # raster pixels per point
        image = Image.new("L", (300 * k, 200 * k), 255)
        ImageDraw.Draw(image).rectangle([(240 - 50) * k + 2, (300 - 220) * k + 2, (300 - 50) * k - 2, (300 - 200) * k - 2], fill=0)
        text = [TextLine("שורה ראשונה של טקסט רגיל בעמוד", 545, 800), TextLine("שורה שנייה של טקסט רגיל בעמוד", 545, 780)]
        return make_pdf([PdfPage(lines=[word] + text, rasters=[(50, 100, 300, 200, image)], **{"turn" if turned else "rotate": rotate})])

    def test_layer_text_masks_its_image_on_a_rotated_page(self) -> None:
        for rotate in (0, 90, 180, 270):
            for turned in (False, True):
                with self.subTest(rotate=rotate, turned=turned):
                    doc = heocr.extract(self.page(rotate, turned), tesseract=FAKE_TESSERACT)
                    self._masked(doc)

    def _masked(self, doc) -> None:
        self.assertEqual(doc.pages[0].page_class, "T6")
        # The mask covers the word's ink: nothing is left to OCR, and nothing is read twice.
        self.assertEqual(doc.unread, [])
        self.assertEqual(doc.pages[0].coverage["image_regions_text_in_layer"], 1)
        self.assertEqual([b for b in doc.blocks if "from_image_region" in b.flags], [])
        self.assertIn("מכרזים", doc.text)

    def test_a_rotated_text_layer_page_reports_the_frame_of_its_boxes(self) -> None:
        doc = heocr.extract(self.page(90), ocr=False)
        page = doc.pages[0]
        self.assertEqual((page.width, page.height), (595.0, 842.0))  # the unrotated box, as the blocks are
        self.assertIn("page_rotated", page.flags)
        for block in doc.blocks:
            self.assertLessEqual(block.bbox[2], page.width)
            self.assertLessEqual(block.bbox[3], page.height)


class DisplayedFrameTest(unittest.TestCase):
    """A rotated page whose text is upright on screen (its content turned in user space) is read as
    displayed: the same text, tables and boxes as the same page unrotated."""

    def test_an_upright_page_reads_the_same_at_every_rotation(self) -> None:
        plain = read(page())
        for turn in (90, 180, 270):
            with self.subTest(turn=turn):
                turned = read(page(turn=turn))
                self.assertEqual(shape(turned), shape(plain))
                self.assertEqual(turned.unread, [])
                self.assertEqual((turned.pages[0].width, turned.pages[0].height), (595.0, 842.0))
                self.assertNotIn("text_sideways_on_screen", turned.pages[0].flags)
                self.assertIn("page_rotated", turned.pages[0].flags)

    def test_a_page_sideways_on_screen_is_still_read_and_flagged(self) -> None:
        plain = read(page())
        sideways = read(page(rotate=90))  # upright in user space, sideways on screen
        self.assertEqual(shape(sideways), shape(plain))
        self.assertIn("text_sideways_on_screen", sideways.pages[0].flags)

    def test_a_mixed_page_reads_its_majority_and_its_sideways_words(self) -> None:
        word = "שוליים"  # drawn outside the turn, right to left as Hebrew is: sideways on screen
        extra = [(ch, 60 + 7 * (len(word) - 1 - n), 300) for n, ch in enumerate(word)]
        doc = read(PdfPage(lines=lines_block(BODY * 3, top=760), turn=90, glyphs=extra))
        self.assertNotIn("text_sideways_on_screen", doc.pages[0].flags)
        for line in BODY:
            self.assertIn(line, doc.text)
        # The sideways word is read in its own frame, after the body, not reported unread.
        (rotated,) = [b for b in doc.blocks if "rotated_text" in b.flags]
        self.assertEqual(rotated.text, "שוליים")
        self.assertEqual([u for u in doc.unread if u.reason == "rotated_text_not_read"], [])


if __name__ == "__main__":
    unittest.main()
