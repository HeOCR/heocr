import unittest

from heocr.docx.numbering import Numbering
from heocr.docx.numformat import format_number, hebrew_letter, hebrew_numeral, latin_letter, roman
from tests.synth.docx import abstract_num, lvl, num, numbering_xml


def labels(numbering: Numbering, steps: list[tuple[int, int]]) -> list[str]:
    out = []
    for num_id, ilvl in steps:
        label = numbering.next_label(str(num_id), ilvl)
        out.append(label.text if label else None)
    return out


class HebrewCountingTest(unittest.TestCase):
    def test_hebrew_numerals(self) -> None:
        cases = {
            1: "א", 9: "ט", 10: "י", 11: "יא", 14: "יד", 15: "טו", 16: "טז", 17: "יז", 20: "כ",
            22: "כב", 99: "צט", 100: "ק", 115: "קטו", 116: "קטז", 400: "ת", 401: "תא", 500: "תק",
            915: "תתקטו", 0: "",
        }
        for value, expected in cases.items():
            self.assertEqual(hebrew_numeral(value), expected, value)

    def test_hebrew_alphabet(self) -> None:
        self.assertEqual([hebrew_letter(v) for v in (1, 2, 11, 22, 23, 45)], ["א", "ב", "כ", "ת", "אא", "אאא"])
        self.assertEqual(format_number(23, "hebrew2"), ("אא", ["numbering_hebrew2_beyond_22"]))

    def test_other_formats(self) -> None:
        self.assertEqual(roman(1994), "MCMXCIV")
        self.assertEqual([latin_letter(v) for v in (1, 26, 27, 28)], ["a", "z", "aa", "bb"])
        self.assertEqual(format_number(3, "upperLetter"), ("C", []))
        self.assertEqual(format_number(4, "lowerRoman"), ("iv", []))
        self.assertEqual(format_number(7, "decimalZero"), ("07", []))
        self.assertEqual(format_number(2, "ordinal"), ("2nd", []))
        self.assertEqual(format_number(3, "decimalEnclosedCircle"), ("③", []))
        self.assertEqual(format_number(5, "chicago"), ("**", []))
        self.assertEqual(format_number(5, "cardinalText"), ("5", ["numbering_format_unsupported:cardinalText"]))


class NumberingTest(unittest.TestCase):
    def test_multi_level_with_default_restarts(self) -> None:
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0), lvl(1), lvl(2)])], [num(1, 0)]))
        steps = [(1, 0), (1, 1), (1, 1), (1, 2), (1, 1), (1, 0), (1, 1), (1, 2), (1, 2)]
        self.assertEqual(labels(n, steps), ["1.", "1.1.", "1.2.", "1.2.1.", "1.3.", "2.", "2.1.", "2.1.1.", "2.1.2."])

    def test_hebrew_levels_and_mixed_templates(self) -> None:
        levels = [lvl(0, text="%1."), lvl(1, "hebrew1", "%2."), lvl(2, "hebrew2", "(%3)"), lvl(3, text="%1.%4")]
        n = Numbering.parse(numbering_xml([abstract_num(0, levels)], [num(1, 0)]))
        steps = [(1, 0)] + [(1, 1)] * 16 + [(1, 2), (1, 2), (1, 3)]
        result = labels(n, steps)
        self.assertEqual(result[0], "1.")
        self.assertEqual(result[1:17][-6:], ["יא.", "יב.", "יג.", "יד.", "טו.", "טז."])
        self.assertEqual(result[17:], ["(א)", "(ב)", "1.1"])

    def test_start_values(self) -> None:
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0, start=5), lvl(1, start=0)])], [num(1, 0)]))
        self.assertEqual(labels(n, [(1, 0), (1, 1), (1, 1), (1, 0)]), ["5.", "5.0.", "5.1.", "6."])

    def test_lvl_restart(self) -> None:
        # Level 3 restarts only after level 1 (lvlRestart=1), not after level 2; level 2 never restarts.
        levels = [lvl(0), lvl(1, restart=0), lvl(2, text="%3)", restart=1)]
        n = Numbering.parse(numbering_xml([abstract_num(0, levels)], [num(1, 0)]))
        steps = [(1, 0), (1, 1), (1, 2), (1, 1), (1, 2), (1, 0), (1, 1), (1, 2)]
        self.assertEqual(labels(n, steps), ["1.", "1.1.", "1)", "1.2.", "2)", "2.", "2.3.", "1)"])

    def test_is_lgl_renders_decimal(self) -> None:
        levels = [lvl(0, "upperRoman", "%1."), lvl(1, "decimal", "%1.%2", lgl=True)]
        n = Numbering.parse(numbering_xml([abstract_num(0, levels)], [num(1, 0)]))
        self.assertEqual(labels(n, [(1, 0), (1, 0), (1, 1)]), ["I.", "II.", "2.1"])

    def test_shared_counter_and_start_override(self) -> None:
        nums = [num(1, 0), num(2, 0), num(3, 0, starts={0: 1})]
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0), lvl(1)])], nums))
        # numId 2 continues numId 1's list; numId 3 is a restarted list of its own, and the
        # shared list continues around it.
        steps = [(1, 0), (1, 0), (2, 0), (3, 0), (3, 1), (3, 0), (1, 0), (3, 0)]
        self.assertEqual(labels(n, steps), ["1.", "2.", "3.", "1.", "1.1.", "2.", "4.", "3."])

    def test_start_override_on_every_level_leaves_the_shared_list_alone(self) -> None:
        nums = [num(1, 0), num(2, 0, starts={0: 1, 1: 1, 2: 1})]
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0), lvl(1), lvl(2)])], nums))
        steps = [(1, 0), (1, 0), (1, 1), (2, 2), (1, 2)]
        self.assertEqual(labels(n, steps), ["1.", "2.", "2.1.", "1.1.1.", "2.1.1."])

    def test_level_override_replaces_definition(self) -> None:
        nums = [num(1, 0), num(2, 0, levels={0: lvl(0, "hebrew1", "%1)")})]
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0)])], nums))
        self.assertEqual(labels(n, [(1, 0), (2, 0)]), ["1.", "ב)"])

    def test_num_style_link(self) -> None:
        abstracts = [abstract_num(0, [], num_style_link="TenderList"), abstract_num(1, [lvl(0, "hebrew1", "%1.")], style_link="TenderList")]
        n = Numbering.parse(numbering_xml(abstracts, [num(4, 0)]))
        self.assertEqual(labels(n, [(4, 0), (4, 0)]), ["א.", "ב."])

    def test_level_for_style(self) -> None:
        levels = [lvl(0, pstyle="Heading1"), lvl(1, pstyle="Heading2")]
        n = Numbering.parse(numbering_xml([abstract_num(0, levels)], [num(1, 0)]))
        self.assertEqual(n.level_for_style("1", "Heading2"), 1)
        self.assertIsNone(n.level_for_style("1", "Normal"))

    def test_removed_or_unknown_numbering(self) -> None:
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0)])], [num(1, 0)]))
        self.assertIsNone(n.next_label("0", 0))
        self.assertIsNone(n.next_label("77", 0))
        self.assertIsNone(Numbering.parse(None).next_label("1", 0))

    def test_unused_parent_is_flagged(self) -> None:
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0), lvl(1)])], [num(1, 0)]))
        label = n.next_label("1", 1)
        self.assertEqual(label.text, "1.1.")
        self.assertIn("numbering_parent_unused", label.flags)
        # The parent shown as 1 counts as used: the next level-0 item is 2.
        self.assertEqual(labels(n, [(1, 1), (1, 0), (1, 1)]), ["1.2.", "2.", "2.1."])

    def test_bullets_and_suffix(self) -> None:
        levels = [lvl(0, "bullet", "", suff="space"), lvl(1, "bullet", "o")]
        n = Numbering.parse(numbering_xml([abstract_num(0, levels)], [num(1, 0)]))
        first = n.next_label("1", 0)
        self.assertEqual((first.text, first.suffix, first.flags), ("•", "space", ("bullet_symbol_mapped",)))
        self.assertEqual(n.next_label("1", 1).text, "o")

    def test_missing_start_is_zero(self) -> None:
        n = Numbering.parse(numbering_xml([abstract_num(0, [lvl(0, start=None)])], [num(1, 0)]))
        self.assertEqual(labels(n, [(1, 0), (1, 0)]), ["0.", "1."])


if __name__ == "__main__":
    unittest.main()
