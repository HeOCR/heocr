"""Word breaks around punctuation in a visual line (``lines._units``), with real glyph gaps.

A comma or a label's full stop hugs one word and has a word space on its other side. In a
right-to-left line the word it hugs is on its right, so the space is on its left: joining by the
glyph alone ("no space before a comma") dropped that space ("תאגיד,למעט", "1.רכב").
"""

import unittest

from heocr import repairs
from heocr.pdf.lines import Atom, BuiltLine, carry_latin_context, logical_text

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
    def test_uniform_letter_spacing_in_justified_hebrew_keeps_word_breaks(self) -> None:
        for expected in ("פרחים מפוזרים בשביל הירוק", "החלון נשאר פתוח"):
            for tracking, word_gap in ((2.6, 8.5), (-1.2, 1.0), (-2.0, -1.3), (0, WORD_SPACE)):
                with self.subTest(expected=expected, tracking=tracking):
                    parts = []
                    for word in expected[::-1].split():
                        if parts:
                            parts.append(word_gap)
                        for index, char in enumerate(word):
                            if index:
                                parts.append(tracking)
                            parts.append(char)
                    self.assertEqual(text(visual(*parts)), expected)

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

    def test_a_latin_references_sub_levels_stay_in_its_run(self) -> None:
        # dev (eval, PR #75): "Article 76(1)" and "§75(1)" inside a Hebrew paragraph. A sub-level after a
        # number is part of the Latin run, read "(1)" as shown, however the producer mapped its glyphs.
        self.assertEqual(text(visual("Article", WORD_SPACE, "76", HUG, "(", HUG, "1", HUG, ")", WORD_SPACE, "ףיעס")), "סעיף Article 76(1)")
        self.assertEqual(text(visual("Article", WORD_SPACE, "76", HUG, ")", HUG, "1", HUG, "(", WORD_SPACE, "יפל")), "לפי Article 76(1)")
        self.assertEqual(text(visual("75", HUG, "(", HUG, "1", HUG, ")", HUG, "§", WORD_SPACE, "ףיעס")), "סעיף §75(1)")
        self.assertEqual(text(visual("GWB", WORD_SPACE, "§", HUG, "130", HUG, "(", HUG, "2", HUG, ")", WORD_SPACE, "ףיעס")), "סעיף GWB §130(2)")
        # Inside an outer bracket, with a letter level: the outer pair is the paragraph's, mirrored with the
        # document's other brackets when its producer maps the glyph (rtl_bracket_order).
        line = text(visual("(", HUG, "Article", WORD_SPACE, "77", HUG, "(", HUG, "2", HUG, ")", HUG, "(", HUG, "a", HUG, ")", HUG, ")", WORD_SPACE, "ףיעס"))
        self.assertEqual(line, "סעיף )Article 77(2)(a)(")
        self.assertEqual(repairs.rtl_bracket_order(line)[0], "סעיף (Article 77(2)(a))")
        # Hebrew around the numbers keeps the rule for Hebrew text: "סעיף 3(4)(ב)".
        self.assertEqual(text(visual("(", HUG, "ב", HUG, ")", HUG, "(", HUG, "4", HUG, ")", HUG, "3", WORD_SPACE, "ףיעס")), "סעיף 3)4()ב(")

    def test_square_bracket_sublevel_keeps_its_place_inside_a_latin_reference(self) -> None:
        for left, right in (("[", "]"), ("]", "["), ("]", "]")):
            with self.subTest(left=left, right=right):
                atoms = visual("Map", WORD_SPACE, "47", HUG, left, HUG, "6", HUG, right, WORD_SPACE, "יפל")
                self.assertEqual(text(atoms), "לפי Map 47[6]")

        self.assertEqual(repairs.mirror_brackets("לפי Map 47[6] ]ב["), "לפי Map 47[6] [ב]")
        self.assertEqual(repairs.mirror_brackets("לפי Map 47[6)"), "לפי Map 47]6(")

    def test_a_latin_parenthetical_whose_brackets_map_the_same_is_one_pair(self) -> None:
        # dev: a producer gave both brackets of a Latin parenthetical as "(" (and of others both as ")"). The
        # brackets hugging the Latin run are its pair; the right one opens it.
        self.assertEqual(text(visual(",", HUG, "(", HUG, "Annual", WORD_SPACE, "Report", HUG, "(", WORD_SPACE, "חוד")), "דוח (Annual Report),")
        self.assertEqual(text(visual(",", HUG, ")", HUG, "Field", WORD_SPACE, "Study", HUG, ")", WORD_SPACE, "רקחמ")), "מחקר (Field Study),")
        self.assertEqual(text(visual(";", HUG, "(", HUG, "Article", WORD_SPACE, "77", HUG, "(", HUG, "2", HUG, ")", HUG, "(", HUG, "a", HUG, "(", HUG, "(",
                                     WORD_SPACE, "רומאכ")), "כאמור (Article 77(2)(a));")
        # Brackets mapped one each way are left to the document's bracket decision, as before.
        self.assertEqual(text(visual("(", HUG, "Annex", WORD_SPACE, "A", HUG, ")", WORD_SPACE, "האר")), "ראה )Annex A(")

    def test_a_line_continuing_a_latin_reference_keeps_its_sub_level(self) -> None:
        # dev (eval, PR #75): "(Art. 76(2))" wrapped in a narrow table cell leaves "76(2))" alone on its
        # line, with no Latin letter: the bracket rule for Hebrew text read it "(2)76". Continuing the
        # line before it, it is read as Latin text.
        second = visual("76", HUG, "(", HUG, "2", HUG, ")", HUG, ")")
        self.assertEqual(logical_text(second, True)[0], "))2(76")
        self.assertEqual(logical_text(second, True, latin_lead=True)[0], "76(2))")
        self.assertEqual(repairs.rtl_bracket_order("76(2)) לעניין", lead=True)[0], "76(2)) לעניין")

    def test_a_paragraph_or_cell_carries_latin_context_to_its_next_line(self) -> None:
        first = visual(".", HUG, "Art", WORD_SPACE, "(", WORD_SPACE, "יפל")
        second = visual("76", HUG, "(", HUG, "2", HUG, ")", HUG, ")")
        lines = [BuiltLine(logical_text(a, True)[0], [0, 0, 1, 1], "rtl", a) for a in (first, second)]
        self.assertEqual(lines[1].text, "))2(76")
        carry_latin_context(lines)
        self.assertEqual(lines[1].text, "76(2))")
        # A line after Hebrew text is left as built.
        lines = [BuiltLine("סעיף", [0, 0, 1, 1], "rtl", []), BuiltLine(logical_text(second, True)[0], [0, 0, 1, 1], "rtl", second)]
        carry_latin_context(lines)
        self.assertEqual(lines[1].text, "))2(76")

    def test_brackets_drawn_left_to_right_after_a_number_keep_their_run(self) -> None:
        # In an LTR line nothing changes: "11(5)" drawn left to right reads as drawn.
        self.assertEqual(text(visual("11", HUG, "(", HUG, "5", HUG, ")"), rtl=False), "11(5)")

    def test_numbers_drawn_left_to_right_keep_one_run(self) -> None:
        # The comma touches the left number: drawn in reading order, left to right.
        self.assertEqual(text(visual("1", HUG, ",", WORD_SPACE, "2")), "1, 2")
        self.assertEqual(text(visual("1,000")), "1,000")



def _cells(*cells: tuple[str, float], y: float = 100.0, width: float = 5.0) -> list:
    """Atoms of one row: each (text, right edge) cell drawn left to right from its right edge back."""
    out, index = [], 0
    for text, right in cells:
        x = right - width * len(text)
        for ch in text:
            out.append(Atom(ch, x, y, x + width, y + 10.0, 10.0, index))
            x += width
            index += 1
    return out


class LetterFreeRowTest(unittest.TestCase):
    """A table row of only amounts, dates and codes has no letter to set its direction: it takes the
    document's. All values are invented."""

    def test_an_all_number_row_takes_the_documents_direction(self) -> None:
        from heocr.pdf.lines import build_lines

        row = _cells(("58,400", 300.0), ("12.12.2021", 255.0))  # the amount cell on the right, one cell gap apart
        self.assertEqual([l.text for l in build_lines(row, default_rtl=True).lines], ["58,400 12.12.2021"])  # rightmost first
        self.assertEqual([l.text for l in build_lines(row, default_rtl=False).lines], ["12.12.2021 58,400"])
        self.assertEqual([l.text for l in build_lines(row).lines], ["12.12.2021 58,400"])  # no default given: as before

    def test_rows_with_letters_keep_their_own_vote(self) -> None:
        from heocr.pdf.lines import build_lines

        latin = _cells(("Total", 300.0), ("12", 255.0))
        self.assertEqual(build_lines(latin, default_rtl=True).lines[0].direction, "ltr")  # Latin letters: left to right
        hebrew = _cells(("סכום", 300.0), ("12", 255.0))
        self.assertEqual(build_lines(hebrew, default_rtl=False).lines[0].direction, "rtl")  # Hebrew letters: right to left


class CellGapTest(unittest.TestCase):
    """Cells set side by side in a row, closer than a column gap: no direction run crosses the gap between
    them. All values are invented."""

    def test_a_code_and_numbers_keep_their_cells_order_in_an_rtl_row(self) -> None:
        from heocr.pdf.lines import build_lines

        # A Hebrew cell (atoms are drawn in visual order, so it is written reversed here), then, one column gap
        # to its left, a code, a count and a date, a cell gap apart.
        row = _cells(("את", 400.0), ("AB-12", 300.0), ("57", 255.0), ("3.4.2020", 230.0))
        self.assertEqual([l.text for l in build_lines(row).lines], ["תא", "AB-12 57 3.4.2020"])  # it was "3.4.2020 57 AB-12"

    def test_a_code_among_amounts_follows_the_document(self) -> None:
        from heocr.pdf.lines import build_lines

        row = _cells(("CN-24", 300.0), ("5,100", 255.0))
        self.assertEqual([l.text for l in build_lines(row, default_rtl=True).lines], ["CN-24 5,100"])
        self.assertEqual([l.text for l in build_lines(row, default_rtl=False).lines], ["5,100 CN-24"])

    def test_a_row_of_latin_words_stays_left_to_right(self) -> None:
        from heocr.pdf.lines import build_lines

        row = _cells(("Total", 300.0), ("Net", 255.0))
        self.assertEqual([(l.text, l.direction) for l in build_lines(row, default_rtl=True).lines], [("Net Total", "ltr")])

    def test_a_word_space_is_not_a_cell_gap(self) -> None:
        from heocr.pdf.lines import build_lines

        # Latin words in a Hebrew line, a wide word space (half a box height) apart: one Latin run, in order.
        row = _cells(("בתכ", 400.0), ("Net", 370.0), ("AB-12", 350.0))
        self.assertEqual([l.text for l in build_lines(row, default_rtl=True).lines], ["כתב AB-12 Net"])


    def test_latin_words_far_apart_do_not_split(self) -> None:
        from heocr.pdf.lines import build_lines

        # An OCR layer can set word boxes far apart: words without digits stay one Latin run.
        row = _cells(("בתכ", 400.0), ("tamen", 365.0), ("nunc", 330.0))
        self.assertEqual([l.text for l in build_lines(row, default_rtl=True).lines], ["כתב nunc tamen"])

    def test_a_latin_reference_far_apart_does_not_split(self) -> None:
        from heocr.pdf.lines import build_lines

        # "Ps 3, 4" in a Hebrew line, its words set far apart: one Latin run, in order.
        row = _cells(("בתכ", 320.0), ("4", 300.0), ("3,", 285.0), ("Ps", 265.0))
        self.assertEqual([l.text for l in build_lines(row, default_rtl=True).lines], ["כתב Ps 3, 4"])


class NumberBesideLatinPairTest(unittest.TestCase):
    """A year set beside a Latin parenthetical in an RTL line, outside its brackets (an invented line).

    LibreOffice draws "משנת 2018 (Assigning responsibilities), ..." right to left and writes both
    brackets of the pair as ")" in its layer. Read as one left-to-right run with the Latin text, the year
    came out after it, with a bracket each side: "משנת Assigning responsibilities) 2018),"."""

    def line(self, left: str, right: str, *, year_inside_line: bool = False):
        if year_inside_line:  # the year after the parenthetical, logically
            return visual(",", HUG, "2018", WORD_SPACE, left, HUG, "Assigning", WORD_SPACE, "responsibilities", HUG, right, WORD_SPACE, "תנשמ")
        return visual(",", HUG, left, HUG, "Assigning", WORD_SPACE, "responsibilities", HUG, right, WORD_SPACE, "2018", WORD_SPACE, "תנשמ")

    def test_the_year_reads_before_the_parenthetical_whatever_the_layer_calls_the_brackets(self) -> None:
        for left, right in ((")", ")"), ("(", ")"), (")", "("), ("(", "(")):
            with self.subTest(layer=left + right):
                out, direction, flags = logical_text(self.line(left, right), True)
                self.assertEqual(out, "משנת 2018 (Assigning responsibilities),")
                self.assertEqual(direction, "rtl")
                self.assertNotIn("bidi_ambiguous_ltr_run", flags)

    def test_a_year_after_the_parenthetical(self) -> None:
        self.assertEqual(text(self.line(")", ")", year_inside_line=True)), "משנת (Assigning responsibilities) 2018,")

    def test_a_number_inside_the_brackets_stays_in_the_latin_run(self) -> None:
        self.assertEqual(text(visual("(", HUG, "Annual", WORD_SPACE, "Report", WORD_SPACE, "2020", HUG, "(", WORD_SPACE, "חוד")), "דוח (Annual Report 2020)")

    def test_the_documents_bracket_mirroring_keeps_the_pair(self) -> None:
        line = text(self.line(")", ")"))
        self.assertEqual(repairs.mirror_brackets(line), line)


class BracketRepairOnceTest(unittest.TestCase):
    """``rtl_bracket_order`` is decided once per document and changes a span once: mirroring is its own
    inverse, so a second pass would undo the first."""

    def test_a_second_pass_changes_nothing(self) -> None:
        from heocr.contract import Block, Document, Line
        from heocr.pdf.reader import _repair_rtl_brackets

        doc = Document(sha256="0" * 64, size=1, format="pdf", format_evidence="test")
        texts = ["הסעיף )ראו להלן( חל", "המסמך )בנוסחו המעודכן( מצורף", "ההחלטה )מיום ראשון( התקבלה", "משנת 2018 (Assigning responsibilities), חלוקת"]
        for t in texts:
            doc.blocks.append(Block(kind="text", text=t, source="text_layer", direction="rtl", lines=[Line(t, [0, 0, 1, 1], "rtl")], flags=["rtl_brackets_unverified"]))
        _repair_rtl_brackets(doc)
        once = [(b.text, list(b.repairs), sorted(b.flags)) for b in doc.blocks]
        self.assertEqual(once[0][0], "הסעיף (ראו להלן) חל")
        self.assertEqual(once[3][0], "משנת 2018 (Assigning responsibilities), חלוקת")
        self.assertEqual([r["count"] for r in once[0][1]], [1, 1])
        _repair_rtl_brackets(doc)
        self.assertEqual([(b.text, list(b.repairs), sorted(b.flags)) for b in doc.blocks], once)


if __name__ == "__main__":
    unittest.main()
