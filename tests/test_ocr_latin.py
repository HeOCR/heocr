"""Invented Hebrew/English line rereads through the public image entry point."""

from io import BytesIO
import unittest
from unittest import mock

from PIL import Image, ImageDraw

import heocr
from heocr.contract import Document
from heocr.ocr import latin_reviewer_for
from heocr.pdf.ocrlatin import reread_latin_edges
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrWord

BOXES = ([280, 40, 340, 60], [210, 40, 264, 60], [150, 40, 196, 60], [108, 40, 132, 60], [38, 40, 91, 60])
FIRST = ("הזמנה", "גרדן", "אמבר", "42", "בחנות")


def result(texts, boxes):
    words = [OcrWord(text, list(box), 94) for text, box in zip(texts, boxes, strict=True)]
    bbox = [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)]
    return OcrResult([OcrLine(" ".join(texts), bbox, [w.confidence for w in words], words=words)])


def invented_image():
    image = Image.new("L", (400, 150), 255)
    draw = ImageDraw.Draw(image)
    for box in BOXES:
        draw.rectangle(box, fill=65)
    buffer = BytesIO()
    image.save(buffer, format="PNG", dpi=(150, 150))
    return buffer.getvalue()


class LatinRunTest(unittest.TestCase):
    def test_product_records_the_stock_mixed_reviewer_separately_from_the_primary_model(self):
        primary_key = "tesseract:heocr_modern_v1.traineddata"
        doc = Document("0" * 64, 0, "PNG", "invented",
                       components={primary_key: "sha256:" + "a" * 64},
                       _ocr_settings={"cmd": "invented-tesseract", "model": "heocr_modern_v1"})
        reviewer = mock.Mock(models={"heb": "b" * 64, "eng": "c" * 64})
        with mock.patch("heocr.ocr.Tesseract.find", return_value=reviewer) as find:
            self.assertIs(latin_reviewer_for(doc), reviewer)
            self.assertIs(latin_reviewer_for(doc), reviewer)
        find.assert_called_once_with("invented-tesseract", lang="heb+eng", psm=7)
        self.assertEqual(doc.components, {
            primary_key: "sha256:" + "a" * 64,
            "tesseract:latin_review_lang": "heb+eng",
            "tesseract:latin_review:heb.traineddata": "sha256:" + "b" * 64,
            "tesseract:latin_review:eng.traineddata": "sha256:" + "c" * 64,
        })

    def test_latin_edge_read_requires_two_agreeing_identifier_reads(self):
        image = Image.new("L", (400, 250), 255)
        edge = result(("14/24", "א-1-2"), ([30, 20, 75, 32], [80, 20, 155, 32])).lines[0]
        body = [result(("שלום",), ([200, y, 350, y + 12],)).lines[0]
                for y in (65, 100, 135, 170)]
        first = result(("Region-Doc", "14/24"), ([4, 2, 70, 14], [75, 2, 122, 14]))
        different = result(("Region-Doc", "15/24"), ([4, 2, 70, 14], [75, 2, 122, 14]))
        engine = mock.Mock()
        engine.recognize_batch.side_effect = ([first], [different], [first], [first])
        self.assertEqual(reread_latin_edges(image, 300, [edge, *body], engine), 0)
        self.assertEqual(edge.text, "14/24 א-1-2")
        self.assertEqual(reread_latin_edges(image, 300, [edge, *body], engine), 1)
        self.assertEqual(edge.text, "Region-Doc 14/24")
        self.assertEqual(edge.bbox, [30, 20, 148, 32])
        self.assertEqual(edge.latin_runs_reread, 1)
        self.assertEqual([line.text for line in body], ["שלום"] * 4)

    def extract(self, review, *, english=None, first=FIRST, check_numbers="suspect", uncertain=True):
        primary = mock.Mock(lang="heb", psm=3, version="tesseract invented", route=None)
        primary.recognize.return_value = result(first, BOXES)
        if uncertain:
            for word in primary.recognize.return_value.lines[0].words[1:3]:
                word.confidence = 30
        numbers = mock.Mock(lang="eng")
        alternate = result(("Amber", "Garden"), (BOXES[2], BOXES[1])) if english is None else english
        numbers.recognize.side_effect = lambda *args, psm=None, **kwargs: alternate if psm == 3 else OcrResult([])
        reviewer = mock.Mock()
        reviewer.recognize.return_value = review
        with mock.patch("heocr.pdf.reader.engine_for", return_value=primary), \
             mock.patch("heocr.pdf.reader.number_engine_for", return_value=numbers), \
             mock.patch("heocr.pdf.reader.latin_reviewer_for", return_value=reviewer) as find_reviewer:
            doc = heocr.extract(invented_image(), check_numbers=check_numbers)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        return doc, reviewer, find_reviewer

    def test_confirmed_run_uses_ltr_word_order_inside_the_original_rtl_line(self):
        # The mixed reviewer reads the crop starting at (35, 37) in image pixels.
        crop_boxes = [[x - 35, y - 37, right - 35, bottom - 37] for x, y, right, bottom in BOXES]
        review = result(("הזמנה", "Amber", "Garden\u200f", "42", "בחנות"),
                        (crop_boxes[0], crop_boxes[2], crop_boxes[1], crop_boxes[3], crop_boxes[4]))
        doc, reviewer, _ = self.extract(review)
        (block,) = doc.blocks
        self.assertEqual(block.text, "הזמנה Amber Garden 42 בחנות")
        self.assertEqual(block.ocr["latin_runs_reread"], 1)
        self.assertIn("latin_from_second_reading", block.flags)
        self.assertEqual(reviewer.recognize.call_args.kwargs, {"dpi": 150, "psm": 7})

    def test_disagreement_keeps_the_ambiguous_run(self):
        crop_boxes = [[x - 35, y - 37, right - 35, bottom - 37] for x, y, right, bottom in BOXES]
        review = result(("הזמנה", "גרדן", "Amber", "42", "בחנות"), crop_boxes)
        doc, reviewer, _ = self.extract(review)
        self.assertEqual(doc.blocks[0].text, " ".join(FIRST))
        self.assertNotIn("latin_from_second_reading", doc.blocks[0].flags)
        reviewer.recognize.assert_called_once()

    def test_numeric_punctuation_blocks_a_latin_proposal(self):
        first = (*FIRST[:3], "42%", FIRST[-1])
        english = result(("Shop42",), (BOXES[3],))
        doc, _, find_reviewer = self.extract(OcrResult([]), first=first, english=english)
        self.assertEqual(doc.blocks[0].text, " ".join(first))
        find_reviewer.assert_not_called()

    def test_skipping_the_english_page_pass_keeps_the_primary_text(self):
        doc, _, find_reviewer = self.extract(OcrResult([]), check_numbers=False)
        self.assertEqual(doc.blocks[0].text, " ".join(FIRST))
        find_reviewer.assert_not_called()

    def test_confident_hebrew_words_keep_the_primary_text(self):
        crop_boxes = [[x - 35, y - 37, right - 35, bottom - 37] for x, y, right, bottom in BOXES]
        review = result(("הזמנה", "Amber", "Garden", "42", "בחנות"),
                        (crop_boxes[0], crop_boxes[2], crop_boxes[1], crop_boxes[3], crop_boxes[4]))
        doc, _, _ = self.extract(review, uncertain=False)
        self.assertEqual(doc.blocks[0].text, " ".join(FIRST))
