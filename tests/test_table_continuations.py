import io
import unittest
import zipfile

import heocr
from tests.synth.pdf import PdfPage, TextLine, make_pdf

XS = (100, 225, 350, 475)
HEADER = ("כמות", "קטגוריה", "פריט")


def fragment(y, rows, extra=(), header=HEADER):
    ys = [y - 30 * i for i in range(len(rows) + 2)]
    rules = [(XS[c], y, XS[c + 1], y) for y in ys for c in range(3)]
    rules += [(x, a, x, b) for x in XS for a, b in zip(ys, ys[1:])]
    lines = [TextLine(text, XS[c + 1] - 8, ys[r] - 20)
             for r, row in enumerate([header, *rows]) for c, text in enumerate(row)]
    return PdfPage(lines=lines + list(extra), rules=rules)


def sample():
    return make_pdf([
        fragment(150, [("2", "אור", "סגול"), ("9", "צל", "ירוק")]),
        fragment(803, [("4", "צל", "צהוב"), ("7", "אור", "אדום")]),
    ])


class TableContinuationsTest(unittest.TestCase):
    def test_api_preserves_physical_tables_and_joins_both_exports(self):
        data = sample()
        base = heocr.extract(data, ocr=False)
        joined = heocr.extract(data, ocr=False, join_tables=True)
        self.assertEqual(len(base.tables_csv()), 2)
        self.assertEqual(joined.text, base.text)
        blocks = [b for b in joined.blocks if b.kind == "table"]
        self.assertEqual([b.checks["table_continuation"]["part"] for b in blocks], [0, 1])
        (csv,) = joined.tables_csv()
        self.assertEqual(csv.splitlines(), ["פריט,קטגוריה,כמות", "סגול,אור,2", "ירוק,צל,9", "צהוב,צל,4", "אדום,אור,7"])
        self.assertEqual(heocr.validate(joined.to_dict()), [])
        with zipfile.ZipFile(io.BytesIO(joined.to_xlsx())) as file:
            self.assertNotIn("xl/worksheets/sheet2.xml", file.namelist())
            self.assertEqual(file.read("xl/worksheets/sheet1.xml").decode().count("קטגוריה"), 1)

    def test_headings_between_fragments_or_conflicting_headers_abstain(self):
        first = fragment(150, [("2", "אור", "סגול"), ("9", "צל", "ירוק")])
        variants = (
            fragment(710, [("4", "צל", "צהוב")], extra=[TextLine("טבלה אחרת", 450, 767)]),
            fragment(803, [("4", "צל", "צהוב")], header=("מחיר", "קטגוריה", "פריט")),
        )
        for next_page in variants:
            with self.subTest():
                doc = heocr.extract(make_pdf([first, next_page]), ocr=False, join_tables=True)
                self.assertTrue(all("table_continuation" not in b.flags for b in doc.blocks))
                self.assertEqual(len(doc.tables_csv()), 2)
