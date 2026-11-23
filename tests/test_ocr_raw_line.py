"""The explicitly segmented Hebrew line retry preserves first-pass evidence."""
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from heocr.ocr.tesseract import Tesseract


class RawLineRetryTest(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (300, 45), "white")
        ImageDraw.Draw(self.image).rectangle((15, 10, 255, 30), fill=(25, 10, 20))

    def read(self, texts, *, engine_psm=7, override=None, language="heb", requested_lang=None):
        engine = Tesseract(["invented-tesseract"], Path("invented"), language, engine_psm, "fake", "0" * 64, {},
                           requested_lang=requested_lang)
        modes = []
        responses = iter(texts)

        def recognize(source, output, dpi, psm):
            with Image.open(source) as image:
                modes.append((image.mode, engine.psm if psm is None else psm, image.size))
            text = next(responses)
            if text is None:
                return subprocess.CompletedProcess([], 1)
            lines = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
            for i, word in enumerate(text.split(), 1):
                lines.append(f"5\t1\t1\t1\t1\t{i}\t{240-20*i}\t10\t15\t20\t95\t{word}")
            output.with_suffix(".tsv").write_text("\n".join(lines), encoding="utf-8")
            return subprocess.CompletedProcess([], 0)

        with patch.object(engine, "_recognize_call", side_effect=recognize):
            result = engine.recognize(self.image, psm=override)
        return result.lines, modes, engine.failures

    def test_blank_color_line_uses_agreeing_grayscale_reads(self):
        lines, modes, _ = self.read(["", "הענן עובר מעל הגן", "הענן עובר מעל הגן"])
        self.assertTrue(lines[0].raw_line_retry)
        self.assertEqual(lines[0].text, "הענן עובר מעל הגן")
        self.assertEqual([(mode, psm) for mode, psm, _ in modes], [("RGB", 7), ("L", 13), ("L", 7)])

    def test_added_letter_preserves_numbers_and_punctuation(self):
        original = "החדר נצב בכיכר 17."
        expanded = "החדר ניצב בכיכר 17."
        lines, modes, _ = self.read([original, expanded, expanded])
        self.assertEqual(lines[0].text, expanded)
        self.assertTrue(lines[0].raw_line_retry)
        self.assertEqual(modes[:2], [("RGB", 7, (300, 45)), ("RGB", 13, (300, 45))])
        self.assertGreater(modes[2][2][0], 300)
        for altered in ("החדר ניצב בכיכר 71.", "החדר ניצב בכיכר 17,", "החדר ניצב בככר 17."):
            with self.subTest(altered=altered):
                lines, modes, _ = self.read([original, altered])
                self.assertEqual(lines[0].text, original)
                self.assertFalse(lines[0].raw_line_retry)
                self.assertEqual(len(modes), 2)

    def test_disagreement_or_failed_retry_keeps_first_read(self):
        original = "צל הענן ארך באחו"
        expanded = "צל הענן ארוך באחו"
        for other, failed in [("צל הענן ארכך באחו", 0), (None, 1)]:
            with self.subTest(other=other):
                lines, _, failures = self.read([original, expanded, other])
                self.assertEqual(lines[0].text, original)
                self.assertEqual(failures, failed)

    def test_page_engine_and_its_internal_crops_bypass_retry(self):
        for override in (None, 7):
            with self.subTest(override=override):
                _, modes, _ = self.read(["האור על הגבעה"], engine_psm=3, override=override)
                self.assertEqual(len(modes), 1)

    def test_explicit_hebrew_model_alias_keeps_retry(self):
        text = "הגשם ירד בחצר"
        lines, modes, _ = self.read(["", text, text], language="invented-snapshot", requested_lang="heb")
        self.assertEqual(lines[0].text, text)
        self.assertEqual(len(modes), 3)
