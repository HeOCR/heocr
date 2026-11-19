"""Niqqud and cantillation in a text layer: marks on their letter, and marks in slots of their own.

Some OCR layers (Internet Archive's, for one) give every character of a word, its marks included, an equal
share of the word's box, so a mark sits beside its letter instead of on it. The text is invented.
"""

import unicodedata
import unittest
from unittest import mock

import heocr
from tests.synth.pdf import PdfPage, lines_block, make_pdf

POINTED = ["שָׁלוֹם לָכֶם חֲבֵרִים", "בְּרֵאשִׁית הָיָה דָּבָר"]


def read(**page) -> list[str]:
    """The page's lines, in canonical order: marks of different classes on one letter may come in either order."""
    text = heocr.extract(make_pdf([PdfPage(lines=lines_block(POINTED), **page)]), ocr=False).text
    return [unicodedata.normalize("NFD", line) for line in text.splitlines()]


EXPECTED = [unicodedata.normalize("NFD", line) for line in POINTED]


class MarksTest(unittest.TestCase):
    def test_marks_on_their_letter(self) -> None:
        self.assertEqual(read(), EXPECTED)

    def test_marks_in_slots_of_their_own_join_the_letter_before_them(self) -> None:
        # Each mark beside its letter: it joins the letter before it in the stream, and the slot it
        # took is not a gap between words.
        self.assertEqual(read(mark_slots=True), EXPECTED)

    def test_without_stream_order_the_slots_split_the_words(self) -> None:
        with mock.patch("heocr.pdf.reader.MARK_SPLIT_STREAM_ORDER", False):
            lines = read(mark_slots=True)
        self.assertNotEqual(lines, EXPECTED)
        self.assertGreater(sum(len(line.split()) for line in lines), sum(len(line.split()) for line in POINTED))


if __name__ == "__main__":
    unittest.main()
