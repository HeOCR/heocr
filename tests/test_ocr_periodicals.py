import io
import unittest
from unittest import mock

from PIL import Image, ImageDraw

import heocr
from heocr.contract import Block, Document, Line, Page
from heocr.ocr.periodicals import mark_running, plan_page
from heocr.ocr.tesseract import OcrLine, OcrResult, Tesseract

SIZE = (1200, 1000)


def line(text, left, top, width, height=14):
    return OcrLine(text, [left, top, left + width, top + height], [95], ("1", "1"))


def body(text, left, top):
    return [line(f"{text} שורת טקסט מספר {i}", left + i % 2 * 4, top + 25 * i, 270)
            for i in range(6)]


def newspaper():
    # The right and middle lanes share an article. The left lane is independent.
    heading = line("כותרת מרכזית", 440, 117, 666, 28)
    right, middle = body("ימין", 822, 157), body("אמצע", 441, 159)
    next_story = line("כתבה חדשה", 857, 348, 214, 26)
    continuation = body("המשך", 822, 392)
    left_heading = line("ידיעה קצרה", 94, 119, 180, 27)
    left = body("שמאל", 63, 157)
    header = line("שם כתב העת הקבוע", 475, 35, 235, 13)
    footer = line("17", 590, 961, 22, 14)
    return [*left, left_heading, *middle, *right, heading, *continuation, next_story, footer, header]


class PeriodicalTest(unittest.TestCase):
    def test_spanning_article_finishes_before_the_next_right_column_article(self):
        source = newspaper()
        result = plan_page(source, SIZE)
        self.assertIsNotNone(result)
        self.assertEqual(result.columns, 3)
        self.assertEqual([(region.role, region.heading, region.article) for region in result.regions],
                         [("header", False, None), ("body", True, 1), ("body", False, 1),
                          ("body", False, 1), ("body", True, 2), ("body", False, 2),
                          ("body", True, 3), ("body", False, 3), ("footer", False, None)])
        self.assertEqual(result.regions[1].columns, (1, 2))
        self.assertCountEqual([id(item) for region in result.regions for item in region.lines],
                              [id(item) for item in source])
        self.assertTrue(result.regions[2].lines[0].text.startswith("ימין"))
        self.assertTrue(result.regions[3].lines[0].text.startswith("אמצע"))

    def test_narrow_newspaper_gutter_can_support_two_lanes(self):
        source = [line("מעל שני טורים", 100, 95, 985, 29)]
        for i in range(8):
            y = 145 + 24 * i
            source.extend([line(f"שמאל כל מילות השורה {i}", 70, y, 528),
                           line(f"ימין כל מילות השורה {i}", 614, y, 490)])
        result = plan_page(source, SIZE)
        self.assertEqual(result.columns, 2)
        self.assertEqual([region.columns for region in result.regions], [(0, 1), (1,), (0,)])

    def test_two_line_headline_shares_one_article_id(self):
        source = newspaper()
        source.append(line("החלק העליון", 475, 83, 560, 27))
        result = plan_page(source, SIZE)
        self.assertEqual([item.text for item in result.regions[1].lines], ["החלק העליון", "כותרת מרכזית"])
        self.assertEqual(result.regions[1].article, result.regions[3].article)

    def test_plain_columns_and_an_unexplained_crossing_use_the_fallback(self):
        self.assertIsNone(plan_page(body("טור אחד", 822, 150) + body("טור אחר", 63, 150), SIZE))
        source = newspaper() + [line("טקסט המתנגש בטורים השונים", 600, 222, 430)]
        self.assertIsNone(plan_page(source, SIZE))

    def test_image_entry_point_projects_headings_and_roles_without_losing_lines(self):
        source = newspaper()
        model = mock.Mock(spec=Tesseract, lang="heb_rivok", psm=3, route=None, failures=0,
                          version="tesseract 5.5.3")
        model.provenance.return_value = {}
        model.recognize.return_value = OcrResult(source)
        stream = io.BytesIO()
        image = Image.new("L", SIZE, 255)
        draw = ImageDraw.Draw(image)
        for y in range(45, 900, 25):
            for x in range(70, 1100, 32):
                draw.rectangle((x, y, x + 10, y + 10), fill=0)
        image.save(stream, format="PNG")
        opts = dict(ocr_router="v2", ocr_capture_type="scanner", check_numbers=False, repairs=False)
        with mock.patch.object(Tesseract, "find", return_value=model):
            doc = heocr.extract(stream.getvalue(), ocr_layout="periodical", **opts)
            model.recognize.return_value = OcrResult(body("טור יחיד", 400, 150))
            baseline = heocr.extract(stream.getvalue(), ocr_layout="v1", **opts)
            fallback = heocr.extract(stream.getvalue(), ocr_layout="periodical", **opts)
        self.assertEqual(doc.pages[0].signals["ocr_periodical_columns"], 3)
        self.assertEqual([block.kind for block in doc.blocks].count("heading"), 3)
        self.assertTrue(all(block.ocr["periodical_columns"] == [1, 2] for block in doc.blocks
                            if block.text == "כותרת מרכזית"))
        self.assertCountEqual([item.text for block in doc.blocks for item in block.lines],
                              [item.text for item in source])
        self.assertEqual([doc.blocks[index].role for index in (0, -1)], ["header", "footer"])
        record = fallback.to_dict()
        record["options"]["ocr"]["layout"] = "v1"
        self.assertEqual(baseline.to_dict(), record)

    def test_repeated_margin_text_is_kept_as_running_furniture(self):
        doc = Document(sha256="0" * 64, size=1, format="pdf", format_evidence="fixture")
        for i in range(2):
            doc.pages.append(Page(i, 1200, 1000, "T5", "ocr", {"ocr_periodical_columns": 2}))
            for x, y, text in [(110, 50, "שם כתב העת הקבוע"), (110, 940, f"גיליון לדוגמה {i + 1}")]:
                box = [x, y, x + 280, y + 14]
                doc.blocks.append(Block("text", text, page=i, bbox=box, source="ocr",
                                        lines=[Line(text, box)], ocr={"periodical_article": 1}))
        mark_running(doc)
        self.assertEqual([block.role for block in doc.blocks], ["header", "footer", "header", "footer"])
        self.assertTrue(all("ocr_periodical_running_furniture" in block.flags for block in doc.blocks))
        self.assertTrue(all("periodical_article" not in block.ocr for block in doc.blocks))
