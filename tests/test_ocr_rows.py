"""Unruled tables on OCR pages (heocr.pdf.ocrrows): cells given back in rows. Values invented."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from heocr.ocr.tesseract import OcrLine, OcrWord
from heocr.pdf.ocrrows import table_rows

SCALE = 300 / 72  # pixels per point at 300 dpi
H = 40  # a line's height in pixels


def line(words: list[tuple[str, float, float]], top: float, block: str) -> OcrLine:
    """One OCR line of (text, left, right) words, Tesseract's order kept."""
    ws = [OcrWord(text, [x0, top, x1, top + H], 90.0) for text, x0, x1 in words]
    return OcrLine(" ".join(w.text for w in ws), [min(w.bbox[0] for w in ws), top, max(w.bbox[2] for w in ws), top + H],
                   [90.0] * len(ws), (block, "1"), ws)


# Three columns, read by Tesseract as three blocks, one after the other (right to left, the RTL order).
ROWS = [("17", "02.05.2021", "4,310"), ("23", "11.08.2021", "980"), ("31", "29.12.2021", "12,045"), ("42", "06.03.2022", "57")]
X = {0: (2000, 2080), 1: (1500, 1760), 2: (1000, 1150)}  # right column first


def column_major() -> list[OcrLine]:
    out = []
    for col in range(3):
        for r, row in enumerate(ROWS):
            out.append(line([(row[col], *X[col])], 300 + 80 * r, str(col + 1)))
    return out


class OcrRowsTest(unittest.TestCase):
    def test_columns_read_one_after_another_come_back_in_rows(self) -> None:
        lines = column_major()
        tables = table_rows(lines, SCALE, rtl=True)
        self.assertEqual(len(tables), 1)
        table = tables[0]
        texts = [[" ".join(p.text for p in cell) for cell in row] for row in table.rows]
        self.assertEqual(texts, [list(row) for row in ROWS])
        self.assertEqual(len(table.lines), len(lines))
        self.assertEqual(table.duplicates, 0)

    def test_left_to_right_document_reads_columns_left_first(self) -> None:
        table = table_rows(column_major(), SCALE, rtl=False)[0]
        self.assertEqual([" ".join(p.text for p in cell) for cell in table.rows[0]], list(reversed(ROWS[0])))

    def test_a_number_given_twice_is_dropped_once(self) -> None:
        # A number split over two blocks is read again whole, and both pieces then carry it and its box.
        lines = column_major()
        lines += [line([(row[1], *X[1])], 300 + 80 * r, "9") for r, row in enumerate(ROWS)]
        table = table_rows(lines, SCALE, rtl=True)[0]
        self.assertEqual([[" ".join(p.text for p in cell) for cell in row] for row in table.rows], [list(row) for row in ROWS])
        self.assertEqual(table.duplicates, len(ROWS))
        self.assertEqual(len(table.lines), len(lines))

    def test_a_table_read_row_by_row_keeps_tesseracts_order(self) -> None:
        # Each row is one Tesseract line, its cells a wide gap apart: Tesseract read the rows in order already.
        lines = [line([(row[0], *X[0]), (row[1], *X[1]), (row[2], *X[2])], 300 + 80 * r, "1") for r, row in enumerate(ROWS)]
        self.assertEqual(table_rows(lines, SCALE, rtl=True), [])

    def test_a_number_split_over_two_blocks_keeps_its_whole_reading(self) -> None:
        # One piece keeps its part of the date ("02.05."), inside the box of the whole reading the other got.
        lines = column_major()
        lines = [line([(row[1][:6], 1500, 1640)], 300 + 80 * r, "0") for r, row in enumerate(ROWS)] + lines
        table = table_rows(lines, SCALE, rtl=True)[0]
        self.assertEqual([[" ".join(p.text for p in cell) for cell in row] for row in table.rows], [list(row) for row in ROWS])
        self.assertEqual(table.duplicates, len(ROWS))

    def test_leftover_marks_inside_a_number_are_dropped(self) -> None:
        lines = column_major()
        lines = [line([("1.", 1640, 1750)], 300 + 80 * r, "0") for r in range(len(ROWS))] + lines
        table = table_rows(lines, SCALE, rtl=True)[0]
        self.assertEqual([[" ".join(p.text for p in cell) for cell in row] for row in table.rows], [list(row) for row in ROWS])

    def test_overlapping_short_labels_are_not_discarded_as_numeric_marks(self) -> None:
        lines = column_major()
        lines += [line([("אב", 1640, 1750)], 300 + 80 * r, "9") for r in range(len(ROWS))]
        # The overlap leaves two distinct pieces in one band, so reconstruction must
        # decline the ambiguous table and preserve the original OCR lines.
        self.assertEqual(table_rows(lines, SCALE, rtl=True), [])

    def test_prose_in_two_columns_is_not_a_table(self) -> None:
        words_right = [("אחת", 2100, 2160), ("שתיים", 1990, 2080), ("שלוש", 1890, 1970), ("ארבע", 1790, 1870), ("חמש", 1700, 1770)]
        words_left = [("שש", 1300, 1350), ("שבע", 1200, 1280), ("שמונה", 1090, 1180), ("תשע", 1000, 1070), ("עשר", 900, 980)]
        lines = [line(words_right, 300 + 80 * r, "1") for r in range(5)] + [line(words_left, 300 + 80 * r, "2") for r in range(5)]
        self.assertEqual(table_rows(lines, SCALE, rtl=True), [])

    def test_two_rows_are_not_a_table(self) -> None:
        lines = [ln for ln in column_major() if ln.bbox[1] < 300 + 80 * 2]
        self.assertEqual(table_rows(lines, SCALE, rtl=True), [])

    def test_a_row_with_two_pieces_in_one_column_leaves_the_page_as_read(self) -> None:
        lines = column_major()
        # A second piece in the middle column's band of one row, longer than leftover marks and not part
        # of that row's date: the columns are not stable.
        lines.append(line([("QX-418", 1640, 1700)], 300 + 80 * 1, "4"))
        self.assertEqual(table_rows(lines, SCALE, rtl=True), [])

    def test_a_line_crossing_the_table_edge_keeps_the_page_as_read(self) -> None:
        lines = column_major()
        # A long caption line under the table whose first words sit in the table's last row band.
        lines.append(line([("סהכ", 2000, 2080), ("לתשלום", 1500, 1600), ("בשקלים", 1380, 1490), ("כולל", 1290, 1370), ("מעמ", 1200, 1280)], 300 + 80 * 3, "5"))
        tables = table_rows(lines, SCALE, rtl=True)
        for table in tables:
            taken = {id(w) for row in table.rows for cell in row for p in cell for w in p.words}
            for ln in table.lines:
                self.assertTrue(all(id(w) in taken for w in ln.words))



class OcrRowsReaderTest(unittest.TestCase):
    """A scanned page through the reader, with the fake Tesseract reading the table column by column."""

    FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tessdata = Path(tmp.name) / "tessdata"
        tessdata.mkdir()
        (tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
        # A title line, then the table's three columns as three blocks, one after the other.
        for n, (word, x0, x1) in enumerate([("סיכום", 1900, 2080), ("תשלומים", 1600, 1860), ("לשנה", 1400, 1560)], start=1):
            rows.append(f"5\t1\t1\t1\t1\t{n}\t{x0}\t300\t{x1 - x0}\t{H}\t92.0\t{word}")
        for col in range(3):
            x0, x1 = X[col]
            for r, row in enumerate(ROWS):
                rows.append(f"5\t1\t{col + 2}\t1\t{r + 1}\t1\t{x0}\t{500 + 80 * r}\t{x1 - x0}\t{H}\t90.0\t{row[col]}")
        self.tsv = Path(tmp.name) / "columns.tsv"
        self.tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"FAKE_TESSDATA": str(tessdata), "FAKE_TSV": str(self.tsv)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def extract(self):
        from tests.synth.pdf import PdfPage, make_pdf

        pdf = make_pdf([PdfPage(images=[(0, 0, 595, 842)], image_gray=0xFF)])
        return heocr.extract(pdf, tesseract=self.FAKE, ocr_model="heb", check_numbers=False)

    def test_the_table_comes_back_in_rows_after_the_title(self) -> None:
        doc = self.extract()
        self.assertEqual([b.kind for b in doc.blocks], ["text", "table"])
        self.assertEqual(doc.blocks[0].text, "סיכום תשלומים לשנה")
        table = doc.blocks[1]
        self.assertEqual([[c.text for c in row] for row in table.cells], [list(row) for row in ROWS])
        self.assertIn("unruled_table", table.flags)
        self.assertEqual((table.source, table.direction), ("ocr", "rtl"))
        self.assertIn("unruled_tables", doc.pages[0].flags)
        self.assertEqual(doc.text, "סיכום תשלומים לשנה\n" + "\n".join(" | ".join(row) for row in ROWS) + "\n")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_switched_off_the_page_keeps_tesseracts_order(self) -> None:
        with mock.patch("heocr.pdf.reader.OCR_TABLE_ROWS", False):
            doc = self.extract()
        self.assertNotIn("table", [b.kind for b in doc.blocks])
        self.assertNotIn("unruled_tables", doc.pages[0].flags)
        # Column by column: every cell of the first column comes before any of the second.
        self.assertLess(doc.text.index(ROWS[-1][0]), doc.text.index(ROWS[0][1]))

    def test_a_late_paragraph_in_the_same_column_is_put_above_the_following_text(self) -> None:
        self.write_paragraphs([
            (1500, 590, "שורת פתיחה"), (1500, 700, "המשך הטקסט"), (1510, 430, "כותרת מקומית"),
            (300, 390, "טור נוסף"), (300, 460, "המשך הטור"),
        ])
        doc = self.extract()
        self.assertEqual([b.text for b in doc.blocks], ["כותרת מקומית", "שורת פתיחה", "המשך הטקסט", "טור נוסף", "המשך הטור"])
        self.assertIn("reading_order_ocr_adjusted_unverified", doc.pages[0].flags)
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_overlapping_paragraph_boxes_keep_engine_order(self) -> None:
        self.write_paragraphs([(1500, 590, "קטע ראשון"), (1510, 580, "קטע חופף")])
        doc = self.extract()
        self.assertEqual([b.text for b in doc.blocks], ["קטע ראשון", "קטע חופף"])
        self.assertNotIn("reading_order_ocr_adjusted_unverified", doc.pages[0].flags)

    def write_paragraphs(self, paragraphs) -> None:
        rows = [self.tsv.read_text(encoding="utf-8").splitlines()[0]]
        for n, (x, y, text) in enumerate(paragraphs, 1):
            rows.append(f"5\t1\t{n}\t1\t1\t1\t{x}\t{y}\t700\t{H}\t90.0\t{text}")
        self.tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
