"""Reading-order fixtures with invented text and geometry."""

from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import heocr
from heocr.contract import Block, Line
from heocr.pdf.ocrorder import order_across_disjoint_gaps
from heocr.pdf.reader import _OcrLineRecords, _order_ocr_gaps
from tests.synth.pdf import PdfPage, make_pdf
from tests.test_ocr import FAKE


def paragraph(left, right, *tops, text="מסלול מוצל"):
    lines = [Line(text, [left, top, right, top + 10]) for top in tops]
    return Block("text", text="\n".join(line.text for line in lines), page=1,
                 bbox=[left, min(tops), right, max(tops) + 10], lines=lines, source="ocr",
                 ocr={"status": "unverified", "words": len(lines) * 2})


class ParagraphOrderTest(unittest.TestCase):
    def test_reconnects_a_column_and_keeps_every_line_and_owner(self):
        first = paragraph(310, 510, 100, 115)
        side = paragraph(30, 240, 105, 120)
        continuation = paragraph(320, 510, 130, 145)
        next_side = paragraph(35, 240, 135)
        blocks = [first, side, continuation, next_side]
        ordered = order_across_disjoint_gaps(blocks)
        expected = [(index, line) for index in (0, 2, 1, 3) for line in blocks[index].lines]
        self.assertEqual(ordered, expected)
        for (_, actual), (_, line) in zip(ordered, expected):
            self.assertIs(actual, line)

    def test_a_spanning_header_prevents_the_move(self):
        first = paragraph(310, 510, 100)
        heading = paragraph(30, 540, 115)
        continuation = paragraph(320, 510, 125)
        self.assertIsNone(order_across_disjoint_gaps([first, heading, continuation]))

    def test_skips_ambiguous_neighbors(self):
        first = paragraph(310, 510, 100)
        side = paragraph(30, 240, 105)
        close = paragraph(320, 510, 125)
        ambiguous = paragraph(320, 508, 127)
        self.assertIsNone(order_across_disjoint_gaps([first, side, close, ambiguous]))

    def test_does_not_pull_a_line_across_an_overlapping_track(self):
        first = paragraph(310, 510, 100)
        side = paragraph(30, 318, 105)
        continuation = paragraph(320, 510, 125)
        self.assertIsNone(order_across_disjoint_gaps([first, side, continuation]))


    def test_keeps_attached_clause_numbers_with_their_lines(self):
        blocks = [paragraph(300, 490, 100), paragraph(30, 240, 105), paragraph(305, 490, 124)]
        self.assertIsNotNone(order_across_disjoint_gaps(blocks))
        original = blocks[:]
        records = _OcrLineRecords(attached={1})
        self.assertFalse(_order_ocr_gaps(blocks, 0, 1, None, 1, records))
        self.assertEqual(blocks, original)


class ScannedParagraphOrderTest(unittest.TestCase):
    def test_scan_pdf_reorders_and_keeps_box_and_pass_provenance(self):
        rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
        # The last engine paragraph contains one line in each track.
        paragraphs = (
            ((1300, 400, "מבוא קצר", 90), (1300, 465, "הדלת נסגרת", 90)),
            ((180, 420, "שלט מקומי", 88), (180, 483, "נקודת מפגש", 88)),
            ((180, 525, "עלייה במדרגות", 96), (1320, 530, "והשביל פנוי", 67)),
        )
        for block, lines in enumerate(paragraphs, 1):
            for row, (x, y, text, confidence) in enumerate(lines, 1):
                for word, content in enumerate(text.split(), 1):
                    left = x + 360 * (2 - word)
                    rows.append(f"5\t1\t{block}\t1\t{row}\t{word}\t{left}\t{y}\t300\t35\t{confidence}\t{content}")
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "heb.traineddata").write_bytes(b"invented OCR model")
            fixture = folder / "order.tsv"
            fixture.write_text("\n".join(rows), encoding="utf-8")
            image = Image.new("L", (595, 842), 255)
            draw = ImageDraw.Draw(image)
            draw.rectangle((35, 65, 150, 81), fill=0)
            pdf = make_pdf([PdfPage(rasters=[(0, 0, 595, 842, image)])])
            png = BytesIO()
            image.save(png, format="PNG")
            with patch.dict(os.environ, {"FAKE_TESSDATA": tmp, "FAKE_TSV": str(fixture)}):
                os.environ.pop("TESSDATA_PREFIX", None)
                options = {"ocr_model": "heb", "tesseract": FAKE, "check_numbers": False}
                doc = heocr.extract(pdf, **options)
                raster = heocr.extract(png.getvalue(), **options)
        self.assertEqual([b.text for b in doc.blocks], [
            "מבוא קצר\nהדלת נסגרת", "והשביל פנוי", "שלט מקומי\nנקודת מפגש", "עלייה במדרגות",
        ])
        self.assertIn("reading_order_ocr_disjoint_gaps_unverified", doc.pages[0].flags)
        self.assertEqual([b.ocr["words"] for b in doc.blocks], [4, 2, 4, 2])
        self.assertEqual([b.ocr["mean_confidence"] for b in doc.blocks], [90, 67, 88, 96])
        self.assertEqual([b.text for b in raster.blocks],
                         ["\n".join(line[2] for line in group) for group in paragraphs])
        self.assertNotIn("reading_order_ocr_disjoint_gaps_unverified", raster.pages[0].flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        self.assertEqual(heocr.validate(raster.to_dict()), [])
