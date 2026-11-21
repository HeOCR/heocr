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
    # OCR off: these tests are about classes and must not depend on a locally installed Tesseract.
    doc = heocr.extract(make_pdf([page]), ocr=False)
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

    def test_t2_visual_order_stream_is_read(self) -> None:
        doc, record = self.assertClass(PdfPage(lines=lines_block(HEBREW), stream_order="visual"), "T2", "text_layer_visual_order")
        self.assertGreaterEqual(record.signals["stream_final_start_rate"], 0.15)
        self.assertEqual(record.signals["final_start_rate"], 0.0)
        self.assertEqual(record.route, "text_layer")
        self.assertEqual(record.coverage["status"], "read")
        self.assertEqual([l.text for b in doc.blocks for l in b.lines], HEBREW)
        self.assertEqual(doc.unread, [])

    def test_t2_without_spaces_is_read(self) -> None:
        doc, _ = self.assertClass(PdfPage(lines=lines_block(HEBREW), stream_order="visual", real_spaces=False), "T2")
        self.assertEqual([l.text for b in doc.blocks for l in b.lines], HEBREW)

    def test_words_drawn_reversed_are_t2_and_not_read(self) -> None:
        reversed_lines = [" ".join(word[::-1] for word in line.split()) for line in HEBREW]
        doc, record = self.assertClass(PdfPage(lines=lines_block(reversed_lines)), "T2", "reversed_after_line_building")
        self.assertGreaterEqual(record.signals["final_start_rate"], 0.15)
        self.assertEqual(doc.blocks, [])
        self.assertEqual(doc.unread[0].reason, "ocr_route_unavailable")
        self.assertEqual(record.coverage["status"], "unread")

    def test_t3_private_use_font_codes(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), remap={"נ": "", "ם": ""})
        doc, record = self.assertClass(page, "T3", "broken_font_mapping")
        self.assertGreater(record.signals["private_use_chars"], 0)
        self.assertEqual(doc.blocks, [])

    def test_t3_latin1_eth_for_nun_without_repairs(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), remap={"נ": "ð"})
        doc = heocr.extract(make_pdf([page]), ocr=False, repairs=False)
        self.assertEqual(doc.pages[0].page_class, "T3")
        self.assertGreaterEqual(doc.pages[0].signals["latin1_next_to_hebrew"], 3)

    def test_t4_foreign_ocr_layer(self) -> None:
        garbage = [TextLine(t, 50, 780 - 18 * i, rtl=False) for i, t in enumerate(["CMLL LLIL NNOI", "IJLL CMMI LLOO", "NLLI CMLL"])]
        page = PdfPage(lines=garbage, invisible=True, images=FULL_PAGE)
        self.assertClass(page, "T4", "foreign_ocr_layer")

    def test_an_ocr_layer_of_numbers_and_codes_is_not_foreign_by_its_letters(self) -> None:
        # A table of amounts, dates and codes has a few Latin letters and no Hebrew. Its layer may well be right,
        # so its letters do not make it another language's: its digits decide (the reader, with OCR). Values invented.
        from heocr.pdf.classify import classify

        rows = [TextLine(t, 50, 780 - 18 * i, rtl=False) for i, t in enumerate(["QX-418 62 14.03.2022", "TR-905 7 1,250", "MV-31 480 2.11.2021"])]
        doc, record = page_of(PdfPage(lines=rows, invisible=True, images=FULL_PAGE))
        self.assertEqual(record.signals["latin_word_letters"], 0)
        self.assertEqual(classify(record.signals), ("T1", ["text_layer_is_ocr", "ocr_layer_numbers_and_codes"]))
        # Without OCR its digits cannot be checked, and the page is read as foreign, as before.
        self.assertEqual(record.page_class, "T4")
        self.assertIn("foreign_ocr_layer", record.flags)
        self.assertNotIn("text_layer_is_ocr", record.flags)
        latin = dict(record.signals, latin_word_letters=40, latin_word_share=0.6)
        self.assertEqual(classify(latin), ("T4", ["foreign_ocr_layer"]))

    def test_hebrew_ocr_layer_is_read_and_flagged(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), invisible=True, images=FULL_PAGE)
        doc, _ = self.assertClass(page, "T1", "text_layer_is_ocr")
        self.assertEqual(doc.blocks[0].lines[0].text, HEBREW[0])

    def test_ocr_layer_numbers_keep_logical_order_with_either_glyph_direction(self) -> None:
        rows = [
            ("הזמנה מספר", "18/7306", "הגיעה לחנות"),
            ("הטופס נשלח בתאריך", "17.8.2046", "לתיבה הירוקה"),
            ("ההנחה היא", "24.8%", "מסכום הקנייה"),
        ]
        expected = [f"{prefix} {number} {suffix}" for prefix, number, suffix in rows]
        for reflected in (False, True):
            with self.subTest(reflected=reflected):
                lines = []
                for row, (prefix, number, suffix) in enumerate(rows):
                    y = 780 - 18 * row
                    # At 12 pt the fixture font advances 6 pt per glyph and 3 pt per space.
                    number_right = 545 - 6 * len(prefix) + 3 * prefix.count(" ") - 3
                    suffix_right = number_right - 6 * len(number) - 3
                    lines.extend([TextLine(prefix, 545, y), TextLine(number, number_right, y, reflected=reflected),
                                  TextLine(suffix, suffix_right, y)])
                page = PdfPage(lines=lines, invisible=True, images=FULL_PAGE, font_name="GlyphLessFont")
                doc = heocr.extract(make_pdf([page]), ocr=False, repairs=False)
                self.assertIn("text_layer_is_ocr", doc.pages[0].flags)
                self.assertEqual(doc.pages[0].route, "text_layer")
                self.assertEqual([line.text for block in doc.blocks for line in block.lines], expected)
                self.assertEqual(doc.pages[0].coverage["chars_read"], doc.pages[0].coverage["chars_total"])

    def test_t5_image_only(self) -> None:
        doc, record = self.assertClass(PdfPage(images=FULL_PAGE), "T5")
        self.assertEqual(record.signals["image_coverage"], 1.0)
        self.assertEqual(doc.unread[0].reason, "ocr_route_unavailable")

    def test_t5_vector_text(self) -> None:
        self.assertClass(PdfPage(paths=150), "T5", "text_may_be_vector_paths")

    def test_t6_reads_the_text_layer_and_reports_image_regions(self) -> None:
        page = PdfPage(lines=lines_block(HEBREW), images=[(50, 100, 495, 400)])
        doc, record = self.assertClass(page, "T6", "images_may_hold_text")
        self.assertAlmostEqual(record.signals["image_coverage"], 495 * 400 / (595 * 842), places=3)
        self.assertEqual(record.route, "text_layer")
        self.assertEqual(record.coverage["status"], "partial")
        self.assertEqual([l.text for b in doc.blocks for l in b.lines], HEBREW)
        regions = [u for u in doc.unread if u.reason == "image_region_not_read"]
        self.assertEqual(len(regions), 1)
        self.assertEqual([round(v) for v in regions[0].bbox], [50, 342, 545, 742])
        self.assertEqual(doc.to_dict()["unread"][0]["bbox"], [50.0, 342.0, 545.0, 742.0])

    def test_symbol_font_bullets_are_not_t3(self) -> None:
        bulleted = ["\uf0b7 " + line for line in HEBREW] + ["\uf0a7 סעיף אחרון"]
        doc, record = self.assertClass(PdfPage(lines=lines_block(bulleted)), "T1")
        self.assertEqual(record.signals["private_use_chars"], 6)
        self.assertEqual(record.signals["private_use_next_to_hebrew"], 0)
        lines = [l.text for b in doc.blocks for l in b.lines]
        self.assertEqual(lines, ["• " + line for line in HEBREW] + ["• סעיף אחרון"])
        repairs = doc.blocks[0].repairs
        self.assertIn({"rule": "symbol_font_bullet", "from": "U+F0B7", "to": "•", "count": 5}, repairs)
        self.assertIn("bullet_symbol_mapped", doc.blocks[0].flags)

    def test_symbol_font_signs_next_to_hebrew_are_not_broken_letters(self) -> None:
        # Wingdings' U+F071 (a box) set against a Hebrew word, as on a real dev page: a sign, not a letter.
        lines = ["\uf071סעיף " + line for line in HEBREW]
        _, record = self.assertClass(PdfPage(lines=lines_block(lines), font_name="Wingdings-Regular"), "T1")
        self.assertEqual(record.signals["private_use_next_to_hebrew"], 0)
        _, record = self.assertClass(PdfPage(lines=lines_block(lines), font_name="David"), "T3", "broken_font_mapping")
        self.assertGreater(record.signals["private_use_next_to_hebrew"], 0)

    def test_times_and_divide_signs_are_not_broken_letters(self) -> None:
        lines = HEBREW + ["שטח 5×3 מטר ו-6÷2 חלקים", "אורך×רוחב×גובה"]
        _, record = self.assertClass(PdfPage(lines=lines_block(lines)), "T1")
        self.assertEqual(record.signals["latin1_next_to_hebrew"], 0)

    def test_small_logo_stays_t1(self) -> None:
        self.assertClass(PdfPage(lines=lines_block(HEBREW), images=[(500, 780, 40, 40)]), "T1")

    def test_empty_and_page_number_only(self) -> None:
        self.assertClass(PdfPage(), "empty")
        doc, _ = self.assertClass(PdfPage(lines=lines_block(["3"])), "T1")
        self.assertEqual(doc.text, "3\n")

    def test_spread_and_rotation_flags(self) -> None:
        self.assertClass(PdfPage(lines=lines_block(HEBREW, right=1100), width=1190), "T1", "possible_spread")
        _, landscape = self.assertClass(PdfPage(lines=lines_block(HEBREW, top=550), width=842, height=595), "T1")
        self.assertNotIn("possible_spread", landscape.flags)
        self.assertClass(PdfPage(lines=lines_block(HEBREW), rotate=90), "T1", "page_rotated")

    def test_mixed_document_reads_only_text_layer_classes(self) -> None:
        pdf = make_pdf([PdfPage(lines=lines_block(HEBREW)), PdfPage(images=FULL_PAGE), PdfPage(lines=lines_block(HEBREW[:1]))])
        doc = heocr.extract(pdf, ocr=False)
        self.assertEqual([p.page_class for p in doc.pages], ["T1", "T5", "T1"])
        self.assertEqual([u.page for u in doc.unread], [1])
        self.assertIn("unread page 2 (ocr_route_unavailable): class T5; ocr turned off", doc.markdown)


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
