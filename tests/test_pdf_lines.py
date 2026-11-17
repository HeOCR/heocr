"""Word breaks around punctuation in a visual line (``lines._units``), with real glyph gaps.

A comma or a label's full stop hugs one word and has a word space on its other side. In a
right-to-left line the word it hugs is on its right, so the space is on its left: joining by the
glyph alone ("no space before a comma") dropped that space ("תאגיד,למעט", "1.רכב").
"""

import unittest

from heocr.pdf.lines import Atom, logical_text

HEIGHT = 12.0
GLYPH = 5.0
WORD_SPACE = 3.0  # a real font's space advance: wider than a word gap, still "tight"
HUG = 0.3  # side bearings only


def visual(*parts) -> list[Atom]:
    """Atoms left to right: strings are glyphs drawn touching, numbers are the gaps between them."""
    atoms, x, index = [], 100.0, 0
    for part in parts:
        if isinstance(part, (int, float)):
            x += part
            continue
        for ch in part:
            atoms.append(Atom(ch, x, 100.0, x + GLYPH, 100.0 + HEIGHT, HEIGHT, index))
            x += GLYPH + 0.01
            index += 1
    return atoms


def text(atoms, rtl=True) -> str:
    return logical_text(atoms, rtl)[0]


class PunctuationSpacingTest(unittest.TestCase):
    def test_a_comma_in_rtl_text_keeps_the_space_on_its_left(self) -> None:
        # "תאגיד, למעט" drawn right to left: the comma just left of its word, the space beyond it.
        self.assertEqual(text(visual("טעמל", WORD_SPACE, ",", HUG, "דיגאת")), "תאגיד, למעט")

    def test_a_label_full_stop_keeps_the_space_to_the_text(self) -> None:
        self.assertEqual(text(visual("בכר", WORD_SPACE, ".", HUG, "1")), "1. רכב")
        self.assertEqual(text(visual("רומאה", WORD_SPACE, ".", HUG, "1.2")), "1.2. האמור")

    def test_ltr_punctuation_is_unchanged(self) -> None:
        self.assertEqual(text(visual("word", HUG, ",", WORD_SPACE, "next"), rtl=False), "word, next")
        self.assertEqual(text(visual("end", 2.0, "."), rtl=False), "end.")

    def test_tight_punctuation_with_no_closer_side_still_joins(self) -> None:
        # Brackets drawn a little apart from the word on both inner sides stay joined.
        self.assertEqual(text(visual("(", 2.0, "see", 2.0, ")"), rtl=False), "(see)")


if __name__ == "__main__":
    unittest.main()
