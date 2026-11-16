import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from heocr.cli import main
from heocr.ocr.tesseract import parse_tsv
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]
FULL_PAGE = [(0, 0, 595, 842)]
HEBREW = ["המציע יגיש את הצעתו עד המועד", "הקבוע במסמכי המכרז ולא יאוחר", "מהשעה הנקובה בהם על פי ההוראות", "שלהלן ובהתאם לתנאים המפורטים", "נתונים נוספים ניתנים לעיון"]


class OcrRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tessdata = Path(self.tmp.name) / "tessdata"
        self.tessdata.mkdir()
        (self.tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        self.log = Path(self.tmp.name) / "calls.jsonl"
        env = {"FAKE_TESSDATA": str(self.tessdata), "FAKE_LOG": str(self.log)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_image_only_page_is_read_by_ocr(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(images=FULL_PAGE)]), tesseract=FAKE)
        page = doc.pages[0]
        self.assertEqual((page.page_class, page.route, page.coverage["status"]), ("T5", "ocr", "ocr"))
        self.assertEqual([b.text for b in doc.blocks], ["שלום עולם\nשורה", "פסקה"])
        first = doc.blocks[0]
        self.assertEqual(first.source, "ocr")
        self.assertIn("ocr_unverified", first.flags)
        self.assertEqual(first.ocr, {"status": "unverified", "engine": "tesseract", "lang": "heb", "psm": 3, "words": 3, "mean_confidence": 91.83})
        # Boxes are in page points: pixels at 300 dpi times 72/300.
        self.assertEqual([round(v, 2) for v in first.lines[0].bbox], [72.0, 48.0, 504.0, 62.4])
        self.assertEqual(doc.unread, [])

    def test_provenance_has_versions_and_hashes_and_no_paths(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(images=FULL_PAGE)]), tesseract=FAKE)
        components = doc.to_dict()["producer"]["components"]
        self.assertEqual(components["tesseract"], "tesseract 5.9.9")
        self.assertEqual(components["tesseract:psm"], "3")
        expected = hashlib.sha256(b"fake heb model").hexdigest()
        self.assertEqual(components["tesseract:heb.traineddata"], f"sha256:{expected}")
        self.assertNotIn(str(self.tessdata), doc.to_json())
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        recognition = [c for c in self.calls() if c and not c[0].startswith("--")][0]
        self.assertEqual(recognition[recognition.index("-l") + 1], "heb")
        self.assertEqual(recognition[recognition.index("--psm") + 1], "3")

    def test_language_list_is_the_users_choice(self) -> None:
        (self.tessdata / "eng.traineddata").write_bytes(b"fake eng model")
        doc = heocr.extract(make_pdf([PdfPage(images=FULL_PAGE)]), tesseract=FAKE, ocr_lang="heb+eng", ocr_psm=6)
        components = doc.components
        self.assertIn("tesseract:eng.traineddata", components)
        self.assertEqual(doc.blocks[0].ocr["lang"], "heb+eng")
        recognition = [c for c in self.calls() if c and not c[0].startswith("--")][0]
        self.assertEqual(recognition[recognition.index("-l") + 1], "heb+eng")
        self.assertEqual(recognition[recognition.index("--psm") + 1], "6")

    def test_missing_model_is_unavailable_not_raised(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(images=FULL_PAGE)]), tesseract=FAKE, ocr_lang="heb+eng")
        self.assertEqual(doc.unread[0].reason, "ocr_route_unavailable")
        self.assertIn("no eng.traineddata", doc.unread[0].detail)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_no_tesseract(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(images=FULL_PAGE)]), tesseract=str(Path(self.tmp.name) / "no-such-program"))
        self.assertEqual((doc.unread[0].reason, doc.unread[0].detail), ("ocr_route_unavailable", "class T5; tesseract not found"))
        self.assertIn("route_unavailable", doc.pages[0].flags)

    def test_failure_is_reported(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "1"}):
            doc = heocr.extract(make_pdf([PdfPage(images=FULL_PAGE)]), tesseract=FAKE)
        self.assertEqual(doc.unread[0].reason, "ocr_failed")

    def test_foreign_ocr_layer_is_ignored_and_reread(self) -> None:
        garbage = [TextLine(t, 50, 780 - 18 * i, rtl=False) for i, t in enumerate(["CMLL LLIL NNOI", "IJLL CMMI LLOO", "NLLI CMLL"])]
        doc = heocr.extract(make_pdf([PdfPage(lines=garbage, invisible=True, images=FULL_PAGE)]), tesseract=FAKE)
        self.assertEqual(doc.pages[0].page_class, "T4")
        self.assertIn("text_layer_ignored", doc.pages[0].flags)
        self.assertNotIn("CMLL", doc.text)
        self.assertIn("שלום עולם", doc.text)

    def test_t6_regions_go_to_region_ocr(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), images=[(50, 100, 495, 300), (500, 20, 4, 4)])
        doc = heocr.extract(make_pdf([page]), tesseract=FAKE)
        record = doc.pages[0]
        self.assertEqual((record.page_class, record.route), ("T6", "text_layer+ocr_regions"))
        layer = [b for b in doc.blocks if b.source == "text_layer"]
        regions = [b for b in doc.blocks if b.source == "ocr"]
        self.assertEqual([l.text for b in layer for l in b.lines], HEBREW)
        self.assertTrue(regions and all("from_image_region" in b.flags for b in regions))
        top = 842 - 400  # the region's top edge, top-left origin
        self.assertAlmostEqual(regions[0].bbox[1], top + 48.0, places=1)
        self.assertEqual([u.reason for u in doc.unread], ["image_region_too_small"])
        self.assertEqual(record.coverage["status"], "partial")

    def test_ocr_off(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(images=FULL_PAGE)]), ocr=False, tesseract=FAKE)
        self.assertEqual(doc.unread[0].detail, "class T5; ocr turned off")
        self.assertFalse(doc.to_dict()["options"]["ocr"])
        self.assertEqual(self.calls(), [])

    def test_invalid_settings_are_refused_early(self) -> None:
        with self.assertRaises(ValueError):
            heocr.extract(b"%PDF-1.7", ocr_psm=2)
        with self.assertRaises(ValueError):
            heocr.extract(b"%PDF-1.7", ocr_lang="heb;rm")

    def test_cli_options(self) -> None:
        path = Path(self.tmp.name) / "scan.pdf"
        path.write_bytes(make_pdf([PdfPage(images=FULL_PAGE)]))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HEOCR_TESSERACT": ""}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(path), "-f", "json", "--no-ocr"]), 0)
        self.assertEqual(json.loads(out.getvalue())["unread"][0]["reason"], "ocr_route_unavailable")


    def test_low_confidence_region_is_reported_not_emitted(self) -> None:
        tsv = Path(self.tmp.name) / "low.tsv"
        tsv.write_text(
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t10\t20\t20\t12.0\t:\n",
            encoding="utf-8",
        )
        page = PdfPage(lines=lines_block(HEBREW), images=[(50, 100, 495, 300)])
        with mock.patch.dict(os.environ, {"FAKE_TSV": str(tsv)}):
            doc = heocr.extract(make_pdf([page]), tesseract=FAKE)
        self.assertFalse([b for b in doc.blocks if b.source == "ocr"])
        self.assertEqual([u.reason for u in doc.unread], ["image_region_ocr_low_confidence"])


class TsvTest(unittest.TestCase):
    def test_parse(self) -> None:
        from tests.synth.fake_tesseract import DEFAULT_TSV

        result = parse_tsv(DEFAULT_TSV)
        self.assertEqual([line.text for line in result.lines], ["שלום עולם", "שורה", "פסקה"])
        self.assertEqual(result.lines[0].bbox, [300.0, 200.0, 2100.0, 260.0])
        self.assertEqual(result.words, 4)
        self.assertEqual(result.lines[2].paragraph, ("2", "1"))


if __name__ == "__main__":
    unittest.main()
