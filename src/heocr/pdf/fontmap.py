"""``font_private_use_by_ocr``: learning what a font's private-use codes stand for, from the page itself.

Some fonts map Hebrew letters to private-use codes and carry no glyph names to recover them
(the dev split's David fonts: F01A for ם, F8FF for נ). The rule asks the page instead:

1. **Word-level OCR.** The words that hold such a code are cropped from the rendered page and
   read one by one by Tesseract (``--psm 8``).
2. **Acceptance.** A reading is accepted only when it matches the layer's word letter for letter
   at every known position. The broken code's position then shows its letter. Punctuation that
   OCR reads just outside the word is trimmed.
3. **A map per code.** A reading that fails a consistency check below (a letter the font draws
   in its regular form, or one at the wrong place in words) is an OCR confusion and is not
   counted. Each code needs at least ``MIN_ACCEPTED`` counted words, and at least
   ``MIN_AGREEMENT`` of them must give the same letter.
4. **Consistency checks** before a map is applied:
   - the target letter is absent, or almost absent, in its regular form in that font;
   - a code mapped to a final letter (ךםןףץ) appears mostly at the end of a word, and a
     regular letter mostly not at the end;
   - no two codes of one font map to the same letter.
5. **Scope.** Maps are learned per font and per document, never carried between documents,
   and never keyed by font name alone.
6. **Fallback.** A code that stays unresolved leaves its pages T3, and they go to whole-page
   OCR. A font with more than ``MAX_UNRESOLVED`` unresolved codes gets no map at all, rather
   than a mixed reading.

The lead approved the design on 2026-11-16; every threshold is a named constant.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from heocr.pdf.lines import Atom, visual_words
from heocr.textflags import is_private_use

MIN_ACCEPTED = 3
MIN_AGREEMENT = 0.9
MAX_UNRESOLVED = 3
WORDS_PER_CODE = 12  # OCR calls per code, at most
ABSENT_SHARE = 0.02  # "almost absent": regular-form letters at most this share of the code's uses
FINAL_AT_END = 0.8
REGULAR_AT_END = 0.5
CROP_PAD_PT = 1.5
WORD_PSM = 8

_FINALS = frozenset("ךםןףץ")
_HEBREW = re.compile(r"[א-ת]")
_TRIM = ".,:;!?\"'()[]-–־"


@dataclass
class FontMap:
    font: str
    letters: dict[str, str] = field(default_factory=dict)  # code -> letter
    evidence: dict[str, dict] = field(default_factory=dict)  # code -> {accepted, agreeing, letter}
    unresolved: dict[str, str] = field(default_factory=dict)  # code -> reason

    def record(self) -> dict:
        """The document-level log entry: counts and the map, no text."""
        return {
            "rule": "font_private_use_by_ocr",
            "font": self.font,
            "map": {f"U+{ord(code):04X}": letter for code, letter in sorted(self.letters.items())},
            "evidence": {f"U+{ord(code):04X}": ev for code, ev in sorted(self.evidence.items())},
            "unresolved": {f"U+{ord(code):04X}": why for code, why in sorted(self.unresolved.items())},
        }


def broken_codes(words: list[list[Atom]]) -> set[tuple[str, str]]:
    """(font, code) pairs of non-symbol private-use codes that sit next to a Hebrew letter in a word."""
    found = set()
    for word in words:
        for i, atom in enumerate(word):
            if atom.font and is_private_use(atom.text[:1]):
                neighbours = word[max(i - 1, 0) : i] + word[i + 1 : i + 2]
                if any(_HEBREW.match(n.text[:1]) for n in neighbours):
                    found.add((atom.font, atom.text[:1]))
    return found


def logical_letters(word: list[Atom]) -> str:
    """The layer's word in logical order (an RTL Hebrew word read right to left)."""
    return "".join(atom.text for atom in reversed(word))


def align(layer: str, ocr: str, unknown: set[str]) -> dict[int, str] | None:
    """Letters OCR gives at the unknown positions of ``layer``, or None unless all known positions agree."""
    layer = "".join(ch for ch in layer if not ch.isspace())
    ocr = "".join(ch for ch in ocr if not ch.isspace())
    candidates = {ocr, ocr.strip(_TRIM)}
    for left in range(0, 3):
        for right in range(0, 3):
            if left + right < len(ocr):
                candidates.add(ocr[left : len(ocr) - right])
    for candidate in candidates:
        if len(candidate) != len(layer):
            continue
        found: dict[int, str] = {}
        for i, (a, b) in enumerate(zip(layer, candidate)):
            if a in unknown:
                found[i] = b
            elif a != b:
                break
        else:
            if found and all(not is_private_use(v) for v in found.values()):
                return found
    return None


def learn(pages: list[tuple[int, list[Atom]]], render, recognize, font_letters: dict[str, Counter]) -> list[FontMap]:
    """Learn per-font maps for one document.

    ``pages`` holds (page index, atoms with font names). ``render(index)`` returns (image, scale)
    for a page, and ``recognize(image)`` returns OCR lines. ``font_letters`` counts each font's
    regular-form Hebrew letters across the document.
    """
    words_by_page = {index: visual_words(atoms) for index, atoms in pages}
    codes = set()
    for words in words_by_page.values():
        codes |= broken_codes(words)
    if not codes:
        return []
    by_font: dict[str, set[str]] = defaultdict(set)
    for font, code in codes:
        by_font[font].add(code)

    # Where each code sits in its words, and which words carry it.
    uses: dict[tuple[str, str], list[tuple[int, list[Atom], int]]] = defaultdict(list)
    at_end: Counter = Counter()
    for index, words in words_by_page.items():
        for word in words:
            letters = [a for a in word if _HEBREW.match(a.text[:1]) or is_private_use(a.text[:1])]
            for position, atom in enumerate(word):
                key = (atom.font, atom.text[:1])
                if key in codes:
                    uses[key].append((index, word, position))
                    # Visual order: the logical end of an RTL word is its leftmost letter.
                    if letters and atom is letters[0]:
                        at_end[key] += 1

    images: dict[int, tuple] = {}
    maps = []
    for font in sorted(by_font):
        fmap = FontMap(font)
        for code in sorted(by_font[font]):
            key = (font, code)
            # Prefer words with this code as their only broken character.
            sample = sorted(uses[key], key=lambda u: sum(is_private_use(a.text[:1]) for a in u[1]))[:WORDS_PER_CODE]
            readings: Counter = Counter()
            for index, word, _ in sample:
                if index not in images:
                    images[index] = render(index)
                image, scale = images[index]
                box = (
                    int((min(a.x0 for a in word) - CROP_PAD_PT) * scale),
                    int((min(a.y0 for a in word) - CROP_PAD_PT) * scale),
                    int((max(a.x1 for a in word) + CROP_PAD_PT) * scale + 1),
                    int((max(a.y1 for a in word) + CROP_PAD_PT) * scale + 1),
                )
                lines = recognize(image.crop(box))
                text = "".join(line.text for line in lines)
                layer = logical_letters(word)
                found = align(layer, text, {ch for ch in layer if is_private_use(ch)})
                if found is None:
                    continue
                letters = {layer[i]: v for i, v in found.items()}
                if code in letters:
                    readings[letters[code]] += 1
            # A reading the code cannot stand for (a letter the font draws in its regular form, or at
            # the wrong place in words) is an OCR confusion, not a vote: ן read as ו, say.
            regular = font_letters.get(font, Counter())
            ruled_out = {l: why for l in readings if (why := _consistent(l, len(uses[key]), at_end[key], regular))}
            votes = Counter({l: n for l, n in readings.items() if l not in ruled_out})
            accepted = sum(votes.values())
            letter, agreeing = votes.most_common(1)[0] if votes else ("", 0)
            fmap.evidence[code] = {"accepted": accepted, "agreeing": agreeing, "checked": len(sample), "uses": len(uses[key])}
            if ruled_out:
                fmap.evidence[code]["ruled_out"] = sum(readings[l] for l in ruled_out)
            if not votes and ruled_out:
                reason = ruled_out[readings.most_common(1)[0][0]]
            else:
                reason = _check(code, letter, accepted, agreeing, len(uses[key]), at_end[key], regular)
            if reason:
                fmap.unresolved[code] = reason
            else:
                fmap.letters[code] = letter
        # Two codes of one font must not claim the same letter.
        claimed = Counter(fmap.letters.values())
        for code, letter in list(fmap.letters.items()):
            if claimed[letter] > 1:
                del fmap.letters[code]
                fmap.unresolved[code] = "another code of this font maps to the same letter"
        if len(fmap.unresolved) > MAX_UNRESOLVED:
            for code in list(fmap.letters):
                del fmap.letters[code]
                fmap.unresolved[code] = f"more than {MAX_UNRESOLVED} codes of this font are unresolved"
        maps.append(fmap)
    return maps


def _check(code: str, letter: str, accepted: int, agreeing: int, uses: int, at_end: int, regular: Counter) -> str | None:
    if accepted < MIN_ACCEPTED:
        return f"{accepted} accepted words, fewer than {MIN_ACCEPTED}"
    if agreeing / accepted < MIN_AGREEMENT:
        return f"agreement {agreeing}/{accepted} below {MIN_AGREEMENT:.0%}"
    return _consistent(letter, uses, at_end, regular)


def _consistent(letter: str, uses: int, at_end: int, regular: Counter) -> str | None:
    """Why a code used ``uses`` times (``at_end`` of them at a word end) cannot stand for ``letter``; None if it can."""
    if _HEBREW.match(letter):
        if regular.get(letter, 0) > max(1, ABSENT_SHARE * uses):
            return f"the font also has {letter} in its regular form"
        share = at_end / uses if uses else 0.0
        if letter in _FINALS and share < FINAL_AT_END:
            return f"mapped to a final letter but at a word end in only {share:.0%} of uses"
        if letter not in _FINALS and share > REGULAR_AT_END:
            return f"mapped to a regular letter but at a word end in {share:.0%} of uses"
    return None
