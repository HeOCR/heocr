"""Invented fixtures for release-harness checks. The release bundle stays outside CI."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from heocr.contract import Block, Cell, Document, Page
from heocr.regress import CHAIN_VARIANTS, Case, _regressions, inspect_document, load_cases


def block(name, text, page=1, **kwargs):
    return {"id": name, "text": text, "pages": [page], **kwargs}


def sample(gold_blocks, order, body, *, tables=(), chain="mixed_direction", variant="plain", pages=None):
    pages = [Page(0, 100, 100, "T1", "text_layer")] if pages is None else pages
    doc = Document("", 0, "pdf", [], page_count=len(pages), blocks=body, pages=pages)
    gold = {"blocks": gold_blocks, "reading_order": order, "tables": tables, "label_basis": "producer"}
    return inspect_document(Case("invented", chain, variant, len(pages), Path("unused"), "", gold), doc)


class RegressChecksTest(unittest.TestCase):
    def test_logical_order_and_mirroring_are_checked_with_literal_amounts(self):
        value = "USD 12,340.50"
        text = f"בדיקת קובץ לדוגמה ({value}) ועוד שלוש מילים"
        start = text.index(value)
        gold = block("amount", text, ltr_runs=[{"start": start, "end": start + len(value)}],
                     values=[{"text": value}])
        passed = sample([gold], ["amount"], [Block("text", text=text, page=0)])
        self.assertEqual(passed["findings"], [])
        reversal = "בדיקת קובץ לדוגמה )USD 50.340,12( ועוד שלוש מילים"
        failed = sample([gold], ["amount"], [Block("text", text=reversal, page=0)])
        self.assertEqual({item["check"] for item in failed["findings"]}, {
            "bracket_mirroring", "numbers_or_amount_order", "amount_text", "mixed_direction", "logical_text_order",
        })
        moved = "בדיקת קובץ לדוגמה USD 12,340.50 (ועוד) שלוש מילים"
        self.assertIn({"check": "bracket_mirroring", "subject": "amount"},
                      sample([gold], ["amount"], [Block("text", text=moved, page=0)])["findings"])

    def test_paragraph_excludes_neighbouring_label_brackets(self):
        body = "משפט בדיקה שהמצאנו"
        gold = [block("line", body, label_model="1)", label_producer="2)")]
        before = Block("text", text="2)", page=0)
        after = Block("text", text=body + " (טקסט נוסף)", page=0)
        self.assertEqual(sample(gold, ["line"], [before, after])["findings"], [])

    def test_body_order_and_observed_labels_do_not_follow_the_model(self):
        first = "ראש ראשון לפני הסוף"
        second = "גוף אחר אחרי הכותרת"
        gold = [block("head", first, label_model="1.1.", label_producer="2.4."), block("end", second)]
        out = [Block("text", text=second, page=0), Block("heading", text=first, page=0, label="2.4.")]
        self.assertEqual(sample(gold, ["head", "end"], out)["findings"],
                         [{"check": "body_reading_order", "subject": "end"}])
        out[1].label = "1.1."
        self.assertIn({"check": "producer_section_label", "subject": "head"},
                      sample(gold, ["head", "end"], out)["findings"])
        gold[0]["label_producer"] = None
        self.assertEqual(sample(gold, ["head", "end"], out)["unscored"], {"producer_label_unavailable": 1})

    def test_table_cell_positions_and_spans_matter(self):
        gold = [{"id": "table"}, block("c0", "North"), block("c1", "South")]
        table = {"id": "table", "rows": 1, "cells": [
            {"r": 0, "c": 0, "rowspan": 1, "colspan": 2, "text": "North", "paragraphs": ["c0"]},
            {"r": 0, "c": 2, "rowspan": 1, "colspan": 1, "text": "South", "paragraphs": ["c1"]},
        ]}
        actual = Block("table", page=0, cells=[[Cell("North", colspan=2), Cell("South")]])
        self.assertFalse(sample(gold, ["table"], [actual], tables=[table])["findings"])
        actual.cells[0].reverse()
        self.assertEqual(sample(gold, ["table"], [actual], tables=[table])["findings"],
                         [{"check": "table_cells_or_spans", "subject": "table"}])

    def test_p10_checks_reading_route_and_baseline_deltas(self):
        pages = [Page(0, 100, 100, "T1", "text_layer")]
        kept = sample([], [], [], chain="p10", variant="ocr_heb", pages=pages)
        replaced = sample([], [], [], chain="p10", variant="ocr_eng", pages=pages)
        self.assertEqual(kept["findings"][0]["check"], "correct_ocr_layer_not_kept")
        self.assertEqual(replaced["findings"][0]["check"], "wrong_ocr_layer_not_replaced")
        self.assertFalse(_regressions([kept], [kept]))
        self.assertEqual(_regressions([replaced], [kept])[0]["check"], "wrong_ocr_layer_not_replaced")


class RegressBundleTest(unittest.TestCase):
    def test_loader_pins_assets_and_orders_chains(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "invented.pdf"
            pdf.write_bytes(b"invented payload")
            pdf_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            gold = root / "invented.json"
            gold.write_text(json.dumps({"schema": "hdoc_gold.v1", "producer": {"pdf_sha256": pdf_sha}}))
            gold_sha = hashlib.sha256(gold.read_bytes()).hexdigest()
            cases = [
                {"id": variant, "chain": chain, "variant": variant, "pages": 1,
                 "pdf": pdf.name, "pdf_sha256": pdf_sha, "gold": gold.name, "gold_sha256": gold_sha,
                 "producer_pdf_sha256": pdf_sha}
                for chain, variants in reversed(tuple(CHAIN_VARIANTS.items())) for variant in sorted(variants)
            ]
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema": "heocr_regress.v1", "purpose": "synthetic_engineering_qa",
                                           "cases": cases}))
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            self.assertEqual(list(dict.fromkeys(c.chain for c in load_cases(manifest, digest))), list(CHAIN_VARIANTS))
            pdf.write_bytes(b"changed payload")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                load_cases(manifest, digest)
