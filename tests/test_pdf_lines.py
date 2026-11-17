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


class NumbersApartInRtlTest(unittest.TestCase):
    """Two numbers with a space between them in an RTL line are two runs, as an RTL paragraph lays them out."""

    def test_a_list_of_numbers(self) -> None:
        # "1, 2" in an RTL paragraph is shown as "2 ,1": the comma touches the 1, the space is left of it.
        self.assertEqual(text(visual("2", WORD_SPACE, ",", HUG, "1")), "1, 2")

    def test_a_section_number_and_a_note_mark(self) -> None:
        self.assertEqual(text(visual("1", WORD_SPACE, ".", HUG, "1.2")), "1.2. 1")
        self.assertEqual(text(visual("2", WORD_SPACE, "1994")), "1994 2")

    def test_a_full_stop_with_equal_gaps_between_two_numbers_stands_alone(self) -> None:
        # set C pdf_t3_pua_font synth-001: "התשס"ז-2007 . 1123" drawn with a real space advance on
        # both sides of the full stop. It hugged both numbers and W4 made one number, "1123.2007".
        self.assertEqual(text(visual("1123", WORD_SPACE, ".", WORD_SPACE, "2007-ז")), "ז-2007 . 1123")
        # A full stop clearly closer to one side still hugs it.
        self.assertEqual(text(visual("1", WORD_SPACE, ".", 2.0, "1.2")), "1.2. 1")

    def test_a_number_in_brackets_after_a_number(self) -> None:
        # set A synth-039: "סעיף 11(5)" in an RTL line is shown as "(5)11 ףיעס", the brackets drawn
        # mirrored; it was read back "5(11)". The brackets take the paragraph's direction, not a
        # number's: line building gives "11)5(", and the document's rtl_bracket_order repair mirrors it.
        self.assertEqual(text(visual("(", HUG, "5", HUG, ")", HUG, "11")), "11)5(")
        self.assertEqual(text(visual("(", HUG, "5", HUG, ")", HUG, "11", WORD_SPACE, "ףיעס")), "סעיף 11)5(")

    def test_numbers_in_brackets_inside_a_latin_run_are_left_as_before(self) -> None:
        # dev (eval, PR #75): "Article 76(1)" and "§75(1)" inside a Hebrew paragraph. The bracket rule
        # is for digits and punctuation in Hebrew text; in a Latin run the line keeps its former order.
        self.assertEqual(text(visual("Article", WORD_SPACE, "76", HUG, "(", HUG, "1", HUG, ")", WORD_SPACE, "ףיעס")), "סעיף )Article 76(1")
        self.assertEqual(text(visual("75", HUG, "(", HUG, "1", HUG, ")", HUG, "§", WORD_SPACE, "ףיעס")), "סעיף §)75(1")
        # The same shapes inside an outer bracket, with a letter level, and after a Latin act name.
        self.assertEqual(text(visual("(", HUG, "Article", WORD_SPACE, "76", HUG, "(", HUG, "1", HUG, ")", HUG, ")", WORD_SPACE, "ףיעס")),
                         "סעיף ))Article 76(1(")
        self.assertEqual(text(visual("(", HUG, "Article", WORD_SPACE, "77", HUG, "(", HUG, "2", HUG, ")", HUG, "(", HUG, "a", HUG, ")", HUG, ")",
                                     WORD_SPACE, "ףיעס")), "סעיף ))Article 77(2)(a(")
        self.assertEqual(text(visual("GWB", WORD_SPACE, "§", HUG, "130", HUG, "(", HUG, "2", HUG, ")", WORD_SPACE, "ףיעס")), "סעיף )GWB §130(2")
        # Hebrew around the numbers keeps the rule: "סעיף 3(4)(ב)".
        self.assertEqual(text(visual("(", HUG, "ב", HUG, ")", HUG, "(", HUG, "4", HUG, ")", HUG, "3", WORD_SPACE, "ףיעס")), "סעיף 3)4()ב(")

    def test_brackets_drawn_left_to_right_after_a_number_keep_their_run(self) -> None:
        # In an LTR line nothing changes: "11(5)" drawn left to right reads as drawn.
        self.assertEqual(text(visual("11", HUG, "(", HUG, "5", HUG, ")"), rtl=False), "11(5)")

    def test_numbers_drawn_left_to_right_keep_one_run(self) -> None:
        # The comma touches the left number: drawn in reading order, left to right.
        self.assertEqual(text(visual("1", HUG, ",", WORD_SPACE, "2")), "1, 2")
        self.assertEqual(text(visual("1,000")), "1,000")


if __name__ == "__main__":
    unittest.main()
