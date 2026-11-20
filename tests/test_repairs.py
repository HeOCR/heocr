import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import heocr
from heocr import repairs
from heocr.cli import main
from tests.synth.docx import Docx, abstract_num, lvl, num, para
from tests.synth.pdf import PdfPage, lines_block, make_pdf

BULLETED = [" סעיף ראשון", " סעיף שני", " סעיף שלישי"]


def pdf_lines(doc) -> list[str]:
    return [line.text for block in doc.blocks for line in (block.lines or [])]


def bullet_docx() -> bytes:
    d = Docx()
    d.abstracts = [abstract_num(0, [lvl(0, "bullet", "")])]
    d.nums = [num(1, 0)]
    d.body = [para("פריט", num_id=1, ilvl=0)]
    return d.build()


class RuleListTest(unittest.TestCase):
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


class SwitchTest(unittest.TestCase):
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
