"""D3g on OCR'd pages: learned table proposals become grids read cell by cell, like ruled tables (D3e).

The fake engine reads a cell by its grey level, and a stub stands in for TableFormer. Synthetic fixtures only.
"""

from io import StringIO
import unittest
from unittest import mock

import heocr
from heocr.cli import _parser, main
from heocr.ocr.tesseract import OcrLine, OcrResult, Tesseract
from heocr.pdf.proposed import image_spans
from tests import test_ocr_tables as d3e  # its OcrTableTest is not imported by name: it would run here again
from tests.test_ocr_tables import DPI, FAKE, LEVELS, XS, YS, px, scan, scanned_pdf

SCALE = DPI / 72


def borderless(cells=LEVELS):
    """The D3e test table with no rulings: only the grey 'text' blocks."""
    from PIL import Image, ImageDraw

    image = Image.new("L", (px(595), px(842)), 255)
    draw = ImageDraw.Draw(image)
    for r, row in enumerate(cells):
        for c, grey in enumerate(row):
            draw.rectangle([px(XS[c] + 30), px(YS[r] + 12), px(XS[c] + 100), px(YS[r] + 28)], fill=grey)
    return image


def proposal():
    """Cell boxes hugging the blocks (top-left frame, points), columns left to right."""
    cells = [{"row": r, "col": c, "box": [XS[c] + 30, YS[r] + 12, XS[c] + 100, YS[r] + 28]}
             for r in range(3) for c in range(3)]
    return {"bbox": [XS[0], YS[0], XS[-1], YS[-1]], "rows": 3, "cols": 3, "cells": cells}


class Stub:
    def __init__(self, proposals=None, fail=False):
        self.proposals = proposals if proposals is not None else [proposal()]
        self.fail = fail
        self.record = {"proposer": "stub"}
        self.calls = []

    def page_tables(self, body, index, width, height):
        self.calls.append((body[:5], index, width, height))
        if self.fail:
            raise RuntimeError("stub failure")
        return self.proposals


class OcrProposedTest(unittest.TestCase):
    setUp = d3e.OcrTableTest.setUp

    def read(self, image, stub):
        with mock.patch("heocr.tableformer.TableFormerProposer", lambda models: stub):
            return heocr.extract(scanned_pdf(image), tesseract=FAKE, check_numbers=False, tables="tableformer",
                                 tables_models="unused")

    def test_a_borderless_table_in_a_scan_is_read_cell_by_cell(self) -> None:
        without = heocr.extract(scanned_pdf(borderless()), tesseract=FAKE, check_numbers=False)
        self.assertEqual([b for b in without.blocks if b.kind == "table"], [])
        stub = Stub()
        doc = self.read(borderless(), stub)
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertEqual([[c.text for c in row] for row in table.cells],
                         [["שם", "תפקיד", "סכום"], ["ספק א", "מנהל", "100"], ["ספק ב", "עובד", "200"]])
        self.assertIn("tableformer_proposed", table.flags)
        self.assertNotIn("ruled_table", table.flags)
        self.assertIn("tableformer_tables", doc.pages[0].flags)
        # Docling saw the page image itself, as a one-page PDF of the page's size in points.
        ((head, index, width, height),) = stub.calls
        self.assertEqual((head, index), (b"%PDF-", 0))
        self.assertAlmostEqual(width, 595, delta=1)
        self.assertAlmostEqual(height, 842, delta=1)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_learned_cells_supplement_unmasked_page_text(self) -> None:
        image = borderless()
        reader = Tesseract.recognize

        def recognize(engine, raster, *args, **kwargs):
            def scaled(v):
                return round(v * raster.width / 595)

            cell_center = (scaled(XS[0] + 65), scaled(YS[0] + 20))
            if (raster.width >= image.width - 2 and raster.height >= image.height - 2
                    and raster.convert("L").getpixel(cell_center) < 200):
                # The bottom two values are inside the proposal. Its border
                # would also erase the right edge of the first line.
                boxes = [[XS[0] - 25, YS[0] + 4, XS[0] + 20, YS[0] + 10],
                         [XS[0] + 32, YS[0] + 12, XS[0] + 96, YS[0] + 29],
                         [XS[0] + 32, YS[1] + 12, XS[0] + 96, YS[1] + 29]]
                values = ["gloss 457", "original 675", "continued 982"]
                return OcrResult([OcrLine(value, [scaled(v) for v in box], [95])
                                  for box, value in zip(boxes, values, strict=True)])
            return reader(engine, raster, *args, **kwargs)

        with mock.patch.object(Tesseract, "recognize", recognize):
            plain = heocr.extract(scanned_pdf(image), tesseract=FAKE, check_numbers=False)
            doc = self.read(image, Stub())
        supplements = [b for b in doc.blocks if "ocr_table_supplement" in b.flags]
        self.assertEqual(len(supplements), 1)
        self.assertEqual(doc.blocks[:-1], plain.blocks)
        self.assertTrue(doc.text.startswith(plain.text))
        self.assertIn("675", doc.text)
        self.assertIn("982", doc.text)
        self.assertIn("100", doc.tables_csv()[0])

    def test_a_ruled_grid_keeps_first_claim(self) -> None:
        doc = self.read(scan(LEVELS), Stub())
        (table,) = [b for b in doc.blocks if b.kind == "table"]
        self.assertIn("ruled_table", table.flags)
        self.assertNotIn("tableformer_proposed", table.flags)
        self.assertNotIn("tableformer_tables", doc.pages[0].flags)

    def test_a_failing_proposer_leaves_the_page_read(self) -> None:
        doc = self.read(borderless(), Stub(fail=True))
        self.assertIn("table_proposer_failed", doc.flags)
        self.assertIn("table_proposer_failed", [u.reason for u in doc.unread])
        self.assertEqual(doc.pages[0].route, "ocr")
        self.assertEqual([b for b in doc.blocks if b.kind == "table"], [])

    def test_fast_mix_runs_the_explicit_table_stage_on_a_scanned_pdf(self):
        stub = Stub()
        with mock.patch("heocr.tableformer.TableFormerProposer", return_value=stub):
            with mock.patch("heocr.pdf.reader.engine_for", return_value=Tesseract.find(FAKE, lang="heb")):
                doc = heocr.extract(scanned_pdf(borderless()), profile="fast-mix-v2", ocr_capture_type="scanner",
                                     tesseract=FAKE, tables="tableformer", tables_models="unused")
        self.assertEqual(doc.options["ocr"]["profile"]["name"], "fast-mix-v2")
        self.assertEqual(doc.options["tables"], stub.record)
        table, = [block for block in doc.blocks if "tableformer_proposed" in block.flags]
        self.assertEqual(table.cells[1][2].text, "100")
        self.assertIn("100", doc.tables_csv()[0])
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_fast_mix_v3_enables_table_supplements_and_footer_order(self):
        payload = scanned_pdf(borderless())
        with self.assertRaisesRegex(ValueError, "pinned --tables-models"):
            heocr.extract(payload, profile="fast-mix-v3", ocr_capture_type="scanner", tesseract=FAKE)
        stub = Stub()
        with (mock.patch("heocr.tableformer.TableFormerProposer", return_value=stub) as proposer,
              mock.patch("heocr.pdf.reader.engine_for", return_value=Tesseract.find(FAKE, lang="heb"))):
            doc = heocr.extract(payload, profile="fast-mix-v3", ocr_capture_type="scanner",
                                tesseract=FAKE, tables_models="unused")
        proposer.assert_called_once_with("unused")
        self.assertEqual(doc.options["ocr"]["profile"]["name"], "fast-mix-v3")
        self.assertTrue(doc.options["ocr"]["footer_order"])
        self.assertEqual(len([b for b in doc.blocks if "ocr_table_supplement" in b.flags]), 1)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_fast_mix_doc_enables_whole_document_stages_on_scanner(self):
        payload = scanned_pdf(borderless())
        with self.assertRaisesRegex(ValueError, "pinned --tables-models"):
            heocr.extract(payload, profile="fast-mix-doc-v1", ocr_capture_type="scanner", tesseract=FAKE)
        stub = Stub()
        with (mock.patch("heocr.tableformer.TableFormerProposer", return_value=stub) as proposer,
              mock.patch("heocr.pdf.reader.engine_for", return_value=Tesseract.find(FAKE, lang="heb")),
              mock.patch("heocr.pdf.document_roles.apply") as roles):
            doc = heocr.extract(payload, profile="fast-mix-doc-v1", ocr_capture_type="scanner",
                                tesseract=FAKE, tables_models="unused")
        proposer.assert_called_once_with("unused")
        roles.assert_called_once_with(doc)
        self.assertEqual(doc.options["ocr"]["profile"]["name"], "fast-mix-doc-v1")
        self.assertEqual(doc.options["tables"], stub.record)
        self.assertTrue(doc.options["ocr"]["footer_order"])
        self.assertTrue(doc.options["join_tables"])
        self.assertTrue(doc.options["form_fields"])
        self.assertTrue(doc._ocr_settings["form_scan_marks"])
        self.assertTrue(doc._form_grid_recovery)
        with mock.patch("heocr.form_export.export") as export:
            doc.to_forms()
        export.assert_called_once_with(doc, join_pages=True)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_cli_forwards_the_opt_in_and_local_model_directory(self):
        args = ["scan.pdf", "--profile", "fast-mix-v2", "--ocr-capture-type", "scanner",
                "--tables", "tableformer", "--tables-models", "local-models"]
        model_dir = _parser().parse_args(args).tables_models
        with (
            mock.patch("heocr.cli.extract", side_effect=ImportError("install the tables extra")) as extract,
            mock.patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            self.assertEqual(main(args), 1)
            self.assertIn("install the tables extra", stderr.getvalue())
        self.assertEqual(extract.call_args.kwargs["tables"], "tableformer")
        self.assertEqual(extract.call_args.kwargs["tables_models"], model_dir)


class ImageSpansTest(unittest.TestCase):
    def test_rulings_are_not_text_lines(self) -> None:
        image = borderless()
        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        for x in XS:  # vertical rulings only: every pixel row of the table holds some ink
            draw.rectangle([px(x) - 1, px(YS[0]), px(x) + 1, px(YS[-1])], fill=0)
        xs, ys = image_spans(image, SCALE, [XS[0], YS[0], XS[-1], YS[-1]], 160)
        self.assertEqual(len(ys), 3)
        for (top, bottom), y in zip(ys, YS):
            self.assertAlmostEqual(top, y + 12, delta=1)
            self.assertAlmostEqual(bottom, y + 28, delta=1)
        # The blocks' columns are inked; the gaps between them are not.
        for c in range(3):
            self.assertTrue(any(a <= XS[c] + 60 <= b for a, b in xs))
            self.assertFalse(any(a <= XS[c] + 115 <= b for a, b in xs))


if __name__ == "__main__":
    unittest.main()
