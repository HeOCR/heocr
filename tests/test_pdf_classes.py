import unittest

import heocr
from heocr.textflags import text_flags
from tests.synth.pdf import PdfPage, TextLine, lines_block, make_pdf

HEBREW = [
    "המציע יגיש את הצעתו עד המועד",
    "הקבוע במסמכי המכרז ולא יאוחר",
    "מהשעה הנקובה בהם על פי ההוראות",
    "שלהלן ובהתאם לתנאים המפורטים",
    "נתונים נוספים ניתנים לעיון",
]
FULL_PAGE = [(0, 0, 595, 842)]


def page_of(page: PdfPage):
    doc = heocr.extract(make_pdf([page]))
    return doc, doc.pages[0]


class PageClassTest(unittest.TestCase):
    def assertClass(self, page: PdfPage, expected: str, flag: str | None = None):
        doc, record = page_of(page)
        self.assertEqual(record.page_class, expected, record.signals)
        if flag:
            self.assertIn(flag, record.flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        return doc, record

    def test_t1(self) -> None:
        doc, record = self.assertClass(PdfPage(lines=lines_block(HEBREW)), "T1")
        self.assertEqual(record.route, "text_layer")
        self.assertEqual(record.signals["final_start_rate"], 0.0)
        self.assertEqual(record.signals["hebrew_share"], 1.0)
        self.assertEqual(doc.unread, [])

    def test_t2_visual_order_stream(self) -> None:
        doc, record = self.assertClass(PdfPage(lines=lines_block(HEBREW), stream_order="visual"), "T2", "visual_order_layer")
        self.assertGreaterEqual(record.signals["stream_final_start_rate"], 0.15)
        self.assertEqual(doc.blocks, [])
        self.assertEqual(doc.unread[0].reason, "class_T2_no_route")
        self.assertEqual(record.coverage["status"], "unread")

    def test_t3_private_use_font_codes(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), remap={"נ": "", "ם": ""})
        doc, record = self.assertClass(page, "T3", "broken_font_mapping")
        self.assertGreater(record.signals["private_use_chars"], 0)
        self.assertEqual(doc.blocks, [])

    def test_t3_latin1_eth_for_nun(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), remap={"נ": "ð"})
        _, record = self.assertClass(page, "T3", "broken_font_mapping")
        self.assertGreaterEqual(record.signals["latin1_next_to_hebrew"], 3)

    def test_t4_foreign_ocr_layer(self) -> None:
        garbage = [TextLine(t, 50, 780 - 18 * i, rtl=False) for i, t in enumerate(["CMLL LLIL NNOI", "IJLL CMMI LLOO", "NLLI CMLL"])]
        page = PdfPage(lines=garbage, invisible=True, images=FULL_PAGE)
        self.assertClass(page, "T4", "foreign_ocr_layer")

    def test_hebrew_ocr_layer_is_read_and_flagged(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), invisible=True, images=FULL_PAGE)
        doc, _ = self.assertClass(page, "T1", "text_layer_is_ocr")
        self.assertEqual(doc.blocks[0].lines[0].text, HEBREW[0])

    def test_t5_image_only(self) -> None:
        doc, record = self.assertClass(PdfPage(images=FULL_PAGE), "T5")
        self.assertEqual(record.signals["image_coverage"], 1.0)
        self.assertEqual(doc.unread[0].reason, "class_T5_no_route")

    def test_t5_vector_text(self) -> None:
        self.assertClass(PdfPage(paths=150), "T5", "text_may_be_vector_paths")

    def test_t6_text_and_images(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), images=[(50, 100, 495, 400)])
        _, record = self.assertClass(page, "T6", "images_may_hold_text")
        self.assertAlmostEqual(record.signals["image_coverage"], 495 * 400 / (595 * 842), places=3)

    def test_small_logo_stays_t1(self) -> None:
        self.assertClass(PdfPage(lines=lines_block(HEBREW), images=[(500, 780, 40, 40)]), "T1")

    def test_empty_and_page_number_only(self) -> None:
        self.assertClass(PdfPage(), "empty")
        doc, _ = self.assertClass(PdfPage(lines=lines_block(["3"])), "T1")
        self.assertEqual(doc.text, "3\n")

    def test_spread_and_rotation_flags(self) -> None:
        self.assertClass(PdfPage(lines=lines_block(HEBREW, right=1100), width=1190), "T1", "possible_spread")
        self.assertClass(PdfPage(lines=lines_block(HEBREW), rotate=90), "T1", "page_rotated")

    def test_mixed_document_reads_only_t1(self) -> None:
        pdf = make_pdf([PdfPage(lines=lines_block(HEBREW)), PdfPage(images=FULL_PAGE), PdfPage(lines=lines_block(HEBREW[:1]))])
        doc = heocr.extract(pdf)
        self.assertEqual([p.page_class for p in doc.pages], ["T1", "T5", "T1"])
        self.assertEqual([u.page for u in doc.unread], [1])
        self.assertIn("unread page 2 (class_T5_no_route)", doc.markdown)


class TextFlagsTest(unittest.TestCase):
    def test_flags(self) -> None:
        self.assertEqual(text_flags("שלום"), [])
        self.assertIn("unmapped_characters", text_flags("�" * 3))
        self.assertIn("suspicious_glyph_mapping", text_flags("של"))
        self.assertIn("implausible_date", text_flags("מיום 31.02.2024"))
        self.assertEqual(text_flags("מיום 29.02.2024"), [])
        self.assertIn("implausible_time", text_flags("בשעה 25:10"))

    def test_block_carries_text_flags(self) -> None:
        doc = heocr.extract(make_pdf([PdfPage(lines=lines_block(["המועד 31.02.2024 בשעה 12:00"]))]))
        self.assertIn("implausible_date", doc.blocks[0].flags)


if __name__ == "__main__":
    unittest.main()
