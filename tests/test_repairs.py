import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import heocr
from heocr import repairs
from heocr.cli import main
from heocr.contract import Block, Cell, Document, Line
from tests.synth.docx import Docx, abstract_num, lvl, num, para
from tests.synth.pdf import PdfPage, lines_block, make_pdf

BULLETED = [" סעיף ראשון", " סעיף שני", " סעיף שלישי"]


def pdf_lines(doc) -> list[str]:
    return [line.text for block in doc.blocks for line in (block.lines or [])]


class OcrDateBracketTest(unittest.TestCase):
    def test_date_shape_and_validation(self) -> None:
        before, after = repairs.OCR_DATE_BRACKET_ORDER.example
        self.assertEqual(repairs.ocr_date_bracket_order(before)[0], after)
        self.assertEqual(repairs.ocr_date_bracket_order(after), (after, {}))
        self.assertEqual(
            repairs.ocr_date_bracket_order("נפתח 29)  בפברואר (2028,")[0],
            "נפתח (29  בפברואר 2028),",
        )
        for text in (
            "נפתח 29) בפברואר (2027",
            "נפתח 6) בחודש (2028",
            "נפתח 9) במרץ (2028.7",
            "נפתח 9) במרץ\n(2028",
            "נפתח [מחר] 9) במרץ (2028",
        ):
            with self.subTest(text=text):
                self.assertEqual(repairs.ocr_date_bracket_order(text), (text, {}))

    def test_preserves_a_balanced_wrapped_pair_and_pdf_text_layer(self) -> None:
        box = [0, 0, 1, 1]
        middle = "בשעה 9) במרץ (2028 ניפגש"
        for source, rows in (
            ("ocr", ["השער נפתח (אתמול", middle, "ונסכם כרגיל)"]),
            ("ocr", [middle, "המשך שאינו מאוזן("]),
            ("text_layer", [middle]),
        ):
            with self.subTest(source=source, rows=rows):
                text = "\n".join(rows)
                block = Block("paragraph", text, source=source, direction="rtl",
                              lines=[Line(row, box) for row in rows])
                doc = Document("", 0, "pdf", "invented", blocks=[block])
                repairs.repair_ocr_date_brackets(doc)
                self.assertEqual(block.text, text)
                self.assertEqual(pdf_lines(doc), rows)
                self.assertEqual(block.repairs, [])


class MirroredBracketTest(unittest.TestCase):
    def test_swaps_same_type_pairs_without_moving_letters_spaces_or_digits(self) -> None:
        raw = "א)גשר  42(, [ )[מפה]( ], }ים{\r\n)צד(, )שביל("
        expected = "א(גשר  42), [ ([מפה]) ], {ים}\r\n(צד), (שביל)"
        fixed, changes = repairs.mirrored_bracket_pairs(raw)
        self.assertEqual(fixed, expected)
        self.assertEqual(sum(changes.values()), 5)

    def test_stops_at_punctuation_line_breaks_and_same_type_brackets(self) -> None:
        for boundary in ".!?:;,\r\n\u2028":
            with self.subTest(boundary=boundary):
                raw = f")א{boundary}ב("
                self.assertEqual(repairs.mirrored_bracket_pairs(raw), (raw, {}))
                self.assertEqual(repairs.mirrored_bracket_pairs(f")א({boundary}ב)")[0],
                                 f"(א){boundary}ב)")
                self.assertEqual(repairs.mirrored_bracket_pairs(f"(א{boundary})ב(")[0],
                                 f"(א{boundary}(ב)")
        self.assertEqual(repairs.mirrored_bracket_pairs("))עלים((")[0], ")(עלים)(")

    def test_keeps_matched_pairs(self) -> None:
        for raw in ("(עלה) לצד (פרח), [עציץ] ליד [צל]", ")א( )ב("):
            with self.subTest(raw=raw):
                self.assertEqual(repairs.mirrored_bracket_pairs(raw), (raw, {}))
        self.assertEqual(
            repairs.mirrored_bracket_pairs("(עלה) ליד (פרח) וגם )זר(")[0],
            "(עלה) ליד (פרח) וגם (זר)",
        )

    def test_repair_does_not_consume_an_opener_with_a_later_match(self) -> None:
        for closer, opener in ((")", "("), ("]", "["), ("}", "{")):
            raw = f"{closer}א {opener}ב{closer} וגם {closer}ג{opener}"
            expected = f"{closer}א {opener}ב{closer} וגם {opener}ג{closer}"
            with self.subTest(raw=raw):
                fixed, changes = repairs.mirrored_bracket_pairs(raw)
                self.assertEqual(fixed, expected)
                self.assertEqual(sum(changes.values()), 1)

    def test_repairs_table_cells_independently(self) -> None:
        block = Block("table", source="ocr", cells=[[Cell("}25{"), Cell(")קצה"), Cell("המשך(")]])
        doc = Document("", 0, "pdf", "invented", blocks=[block])
        repairs.repair_mirrored_brackets(doc)
        self.assertEqual([cell.text for cell in block.cells[0]], ["{25}", ")קצה", "המשך("])
        self.assertEqual(block.repairs, [{
            "rule": "mirrored_bracket_pairs", "from": "U+007D U+007B", "to": "U+007B U+007D", "count": 1,
        }])


def bullet_docx() -> bytes:
    d = Docx()
    d.abstracts = [abstract_num(0, [lvl(0, "bullet", "")])]
    d.nums = [num(1, 0)]
    d.body = [para("פריט", num_id=1, ilvl=0)]
    return d.build()


class RuleListTest(unittest.TestCase):
    def test_hebrew_ellipsis_leaves_other_dot_patterns_as_read(self) -> None:
        raw = "בבית... שוב\r\nהשער נפתח...\nList... options\nפריט ....... 22\nקוד 20...23\nדגם... 45\nשדה.... מילוי"
        expected = raw.replace("בבית...", "בבית…").replace("נפתח...", "נפתח…")
        self.assertEqual(repairs.ocr_hebrew_ellipsis(raw), (expected, {("...", "…"): 2}))
        self.assertEqual(repairs.ocr_hebrew_ellipsis(expected), (expected, {}))

    def test_every_rule_is_documented_and_its_example_holds(self) -> None:
        self.assertIn("symbol_font_bullet", repairs.RULES)
        for rule in repairs.RULES.values():
            self.assertTrue(rule.reason and rule.evidence and rule.formats)
            before, after = rule.example
            self.assertEqual(getattr(repairs, rule.rule_id)(before)[0], after)

    def test_bullet_rule_needs_its_evidence(self) -> None:
        self.assertEqual(repairs.symbol_font_bullet("ש")[0], "ש")  # not at line start
        self.assertEqual(repairs.symbol_font_bullet("סעיף")[0], "סעיף")  # no space after it
        self.assertEqual(repairs.symbol_font_bullet("")[0], "•")

    def test_hebrew_comma_spacing_is_local_to_each_line(self) -> None:
        raw = "(העציץ פרח)\t,שוב באביב ,\r\nהחלון נסגר ,אבל הדלת פתוחה"
        repaired, changes = repairs.hebrew_comma_spacing(raw)
        self.assertEqual(repaired, "(העציץ פרח), שוב באביב,\r\nהחלון נסגר, אבל הדלת פתוחה")
        self.assertEqual(sum(changes.values()), 3)
        self.assertEqual(repairs.hebrew_comma_spacing(repaired), (repaired, {}))

    def test_hebrew_comma_spacing_preserves_numbers_and_latin_runs(self) -> None:
        text = "מחיר 12 ,345 ו־8,900\nעוד 67 , 890\nרשימה (map , compass) פתוחה\nסוף Chapter IX ,\nMaps ,Compasses"
        self.assertEqual(repairs.hebrew_comma_spacing(text), (text, {}))

    def test_hebrew_comma_spacing_keeps_the_space_after_a_comma(self) -> None:
        raw = "נעצר (לרגע) ,  והמשיך\nנגמר\n ,ואז התחיל"
        repaired, changes = repairs.hebrew_comma_spacing(raw)
        self.assertEqual(repaired, "נעצר (לרגע),  והמשיך\nנגמר\n, ואז התחיל")
        self.assertEqual(changes[" ,", ","], 1)


class SwitchTest(unittest.TestCase):
    def test_mirrored_brackets_in_kept_pdf_text_below_document_threshold(self) -> None:
        raw = "השביל עובר )ליד הגדר("
        expected = "השביל עובר (ליד הגדר)"
        body = make_pdf([PdfPage(lines=lines_block([expected]))])
        off = heocr.extract(body, ocr=False, repairs=False)
        on = heocr.extract(body, ocr=False)
        self.assertEqual(on.pages[0].route, "text_layer")
        self.assertEqual(pdf_lines(off), [raw])
        self.assertEqual(pdf_lines(on), [expected])
        self.assertEqual(on.blocks[0].text, on.blocks[0].lines[0].text)
        self.assertEqual(on.blocks[0].lines[0].bbox, off.blocks[0].lines[0].bbox)
        self.assertEqual(on.blocks[0].repairs[0]["rule"], "mirrored_bracket_pairs")
        self.assertEqual(heocr.validate(on.to_dict()), [])

    def test_kept_pdf_text_preserves_three_literal_periods(self) -> None:
        raw = "בשביל... ליד העץ"
        body = make_pdf([PdfPage(lines=lines_block([raw]))])
        doc = heocr.extract(body, ocr=False)
        self.assertEqual(doc.pages[0].route, "text_layer")
        self.assertEqual(pdf_lines(doc), [raw])
        self.assertEqual(doc.blocks[0].repairs, [])

    def test_hebrew_comma_spacing_in_kept_pdf_lines(self) -> None:
        raw = "ננעל השער , ונותר הפעמון"
        body = make_pdf([PdfPage(lines=lines_block([raw]))])
        off = heocr.extract(body, ocr=False, repairs=False)
        on = heocr.extract(body, ocr=False)
        self.assertEqual(on.pages[0].route, "text_layer")
        self.assertEqual(pdf_lines(off), [raw])
        self.assertEqual(pdf_lines(on), ["ננעל השער, ונותר הפעמון"])
        self.assertEqual(on.blocks[0].text, on.blocks[0].lines[0].text)
        self.assertEqual(on.blocks[0].repairs[0]["rule"], "hebrew_comma_spacing")
        self.assertEqual(heocr.validate(on.to_dict()), [])

    def test_pdf_bullets_on_and_off(self) -> None:
        body = make_pdf([PdfPage(lines=lines_block(BULLETED))])
        on = heocr.extract(body)
        self.assertEqual(pdf_lines(on), ["• סעיף ראשון", "• סעיף שני", "• סעיף שלישי"])
        self.assertEqual(
            on.blocks[0].repairs,
            [
                {"rule": "symbol_font_bullet", "from": "U+F0A7", "to": "•", "count": 1},
                {"rule": "symbol_font_bullet", "from": "U+F0B7", "to": "•", "count": 2},
            ],
        )
        off = heocr.extract(body, repairs=False)
        self.assertEqual(pdf_lines(off), BULLETED)  # the extracted text, byte for byte
        self.assertEqual([b.repairs for b in off.blocks], [[]])
        self.assertFalse(off.to_dict()["options"]["repairs"])
        self.assertTrue(on.to_dict()["options"]["repairs"])

    def test_docx_bullet_label_on_and_off(self) -> None:
        on = heocr.extract(bullet_docx()).blocks[0]
        self.assertEqual((on.label, on.repairs[0]["from"]), ("•", "U+F0B7"))
        self.assertIn("bullet_symbol_mapped", on.flags)
        off = heocr.extract(bullet_docx(), repairs=False).blocks[0]
        self.assertEqual((off.label, off.repairs), ("", []))

    def test_cli_no_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            path.write_bytes(make_pdf([PdfPage(lines=lines_block(BULLETED[:1]))]))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main([str(path), "-q", "-f", "json", "--no-repairs"]), 0)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["options"]["repairs"])
        self.assertEqual(payload["blocks"][0]["text"], BULLETED[0])

    def test_invalid_options_are_rejected(self) -> None:
        payload = heocr.extract(bullet_docx()).to_dict()
        payload["options"] = {"repairs": "yes"}
        self.assertTrue(any("options" in e for e in heocr.validate(payload)))


if __name__ == "__main__":
    unittest.main()
