import io
import unittest
from unittest import mock

from PIL import Image, ImageDraw

import heocr
from heocr.ocr.layout import clip_guard, column_order, normalize_ltr
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrWord, Tesseract
from heocr.pdf.ocrrows import table_rows


def word(text, x, y, width=22):
    return OcrWord(text, [x, y, x + width, y + 12], 94)


def line(words, paragraph=("1", "1")):
    return OcrLine(" ".join(w.text for w in words),
                   [min(w.bbox[0] for w in words), min(w.bbox[1] for w in words),
                    max(w.bbox[2] for w in words), max(w.bbox[3] for w in words)],
                   [w.confidence for w in words], paragraph, list(words))


def page():
    bitmap = Image.new("L", (800, 640), 255)
    draw = ImageDraw.Draw(bitmap)
    for y in range(40, 350, 27):
        for x in range(70, 720, 32):
            draw.rectangle((x, y, x + 10, y + 10), fill=0)
    stream = io.BytesIO()
    bitmap.save(stream, format="PNG")
    return stream.getvalue()


def prose(side, y):
    return line([word(side, 80 if side == "שמאל" else 500, y),
                 word("טקסט", 135 if side == "שמאל" else 557, y),
                 word("תקין", 210 if side == "שמאל" else 650, y)])


class LayoutTest(unittest.TestCase):
    def test_narrow_margin_notes_follow_the_adjacent_body_row(self):
        body = [line([word("מילה", 260, y), word("שורה", 520, y), word("מבחן", 720, y)])
                for y in range(30, 530, 38)]
        notes = [line([word("הערה", 120, body[i].bbox[1] + 2, 65)]) for i in (1, 4, 7, 10)]
        ordered = column_order(body + notes, 800)
        self.assertIsNotNone(ordered)
        for note in notes:
            at = next(n for n, value in enumerate(ordered) if value.bbox == note.bbox)
            self.assertEqual(ordered[at - 1].bbox[1], note.bbox[1] - 2)

    def test_rtl_columns_preserve_spanning_headers(self):
        header = line([word("כותרת", 50, 10, 700)])
        footer = line([word("סיום", 50, 270, 700)])
        pairs = [(prose("שמאל", y), prose("ימין", y + 2)) for y in (45, 83, 121, 159, 197)]
        interleaved = [line for pair in pairs for line in pair]
        result = column_order([header, *interleaved, footer], 800)
        self.assertEqual([value.text.split()[0] for value in result],
                         ["כותרת"] + ["ימין"] * 5 + ["שמאל"] * 5 + ["סיום"])
        self.assertIsNone(column_order([interleaved[0], interleaved[2]], 800))
        self.assertIsNone(column_order([*interleaved, line([word("מעבר", 100, 120, 630)])], 800))

    def test_three_columns_read_each_complete_column_right_to_left(self):
        lines = [
            line([word(side, x, y), word("טקסט", x + 70, y), word("תקין", x + 140, y)])
            for y in range(40, 300, 40)
            for side, x in (("שמאל", 20), ("אמצע", 310), ("ימין", 600))
        ]
        result = column_order(lines, 800)
        self.assertEqual([value.text.split()[0] for value in result],
                         ["ימין"] * 7 + ["אמצע"] * 7 + ["שמאל"] * 7)

    def test_does_not_resort_the_interior_of_already_grouped_columns(self):
        right = [prose("ימין", y) for y in (45, 121, 83, 159, 197)]
        left = [prose("שמאל", y) for y in (45, 83, 121, 159, 197)]
        self.assertIsNone(column_order(right + left, 800))

    def test_ltr_run_uses_visual_geometry_and_preserves_hebrew_anchors(self):
        mixed = line([word("גרסה", 200, 20), word("2026", 140, 20),
                      word("build", 110, 20), word("הותקנה", 55, 20)])
        self.assertEqual(normalize_ltr([mixed]), 1)
        self.assertEqual(mixed.text, "גרסה build 2026 הותקנה")
        self.assertEqual(normalize_ltr([mixed]), 0)
        numbers = line([word("לסעיף", 200, 20), word("2", 140, 20), word("5", 110, 20), word("להלן", 55, 20)])
        self.assertEqual(normalize_ltr([numbers]), 0)

    def test_existing_row_traversal_can_gain_structured_rtl_cells(self):
        lines = [line([word(f"{i}5.00", 40, y), word("סכום", 300, y)])
                 for i, y in enumerate((20, 41, 62), start=1)]
        self.assertEqual(table_rows(lines, 1, True), [])
        result = table_rows(lines, 1, True, layout=True)
        self.assertEqual(len(result), 1)
        self.assertEqual([[cell[0].text for cell in row] for row in result[0].rows],
                         [["סכום", "15.00"], ["סכום", "25.00"], ["סכום", "35.00"]])

    def test_clip_guard_remaps_boxes_and_rejects_lost_text(self):
        bitmap = Image.new("RGB", (180, 80), "white")
        ImageDraw.Draw(bitmap).rectangle((0, 24, 18, 40), fill="black")
        before = OcrResult([line([word("בית", 1, 25), word("שלום", 50, 25)])])
        reread = mock.Mock(return_value=OcrResult([line([word("ובית", 12, 37), word("שלום", 62, 37)])]))
        engine = mock.Mock(recognize=reread)
        padded = clip_guard(engine, bitmap, 288, before, psm=7)
        self.assertEqual(padded.lines[0].text, "ובית שלום")
        self.assertEqual(padded.lines[0].words[0].bbox, [0, 25, 22, 37])
        self.assertEqual(reread.call_args.args[0].size, (204, 104))
        reread.return_value = OcrResult([line([word("בית", 12, 37)])])
        self.assertIsNone(clip_guard(engine, bitmap, 288, before, psm=7))

    def test_clip_guard_without_edge_ink_skips_full_color_conversion(self):
        bitmap = Image.new("RGB", (180, 80), "white")
        ImageDraw.Draw(bitmap).rectangle((12, 24, 18, 40), fill="black")
        engine = mock.Mock()
        with mock.patch.object(bitmap, "convert") as convert:
            self.assertIsNone(clip_guard(engine, bitmap, 288, OcrResult([])))
        convert.assert_not_called()
        engine.recognize.assert_not_called()

    def test_image_route_uses_columns_and_simple_page_retains_record(self):
        interleaved = [prose(side, y) for y in (45, 83, 121, 159, 197)
                       for side in ("שמאל", "ימין")]
        simple = [prose("ימין", y) for y in (45, 83, 121, 159, 197)]
        model = mock.Mock(spec=Tesseract, lang="heb_rivok", psm=3, route=None, failures=0,
                          version="tesseract 5.5.3")
        model.provenance.return_value = {}
        model.recognize.return_value = OcrResult(interleaved)
        opts = dict(ocr_router="v2", ocr_capture_type="scanner", check_numbers=False)
        with mock.patch.object(Tesseract, "find", return_value=model):
            doc = heocr.extract(page(), ocr_layout="v1", **opts)
            model.recognize.return_value = OcrResult(simple)
            baseline = heocr.extract(page(), **opts)
            clean = heocr.extract(page(), ocr_layout="v1", **opts)
        self.assertIn("ocr_layout_rtl_columns", doc.pages[0].flags)
        self.assertEqual([value.split()[0] for value in doc.text.splitlines()], ["ימין"] * 5 + ["שמאל"] * 5)
        record = clean.to_dict()
        self.assertEqual(record["options"]["ocr"].pop("layout"), "v1")
        self.assertEqual(baseline.to_dict(), record)
