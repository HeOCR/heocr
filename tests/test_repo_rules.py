"""D5d: rules about what the repository may hold, enforced on every CI run.

- **No corpus text.** The government corpus is private: no repository file may hold its text.
  CI enforces that no document or image file is committed (every fixture is built in code, from
  invented text). An opt-in check, ``HEOCR_TEST_CORPUS`` (a folder of corpus files), looks for any
  ``SHINGLE``-word passage of the corpus in every tracked file, and reports counts only. Run it
  before a release.
- **Ported code is credited generally** (Shay's decision, 2026-11-18): no file names the internal
  tools or repositories it came from, and the institute's name appears only in NOTICE's general line.
"""

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = re.compile(r"\.(pdf|docx?|dotx|xlsx?|xlsm|pptx?|od[tsp]|rtf|html?|xhtml|epub|eml|msg|png|jpe?g|tiff?|gif|bmp|webp|jp2|zip)$", re.I)
# The names of the ported code's sources and of the volunteer's project, assembled from pieces so
# that this file does not itself hold them.
NAMES = "|".join(a + b for a, b in (("ten", "dex"), ("gov", "il"), ("ams", "ter"), ("yona", "tan")))
ORIGIN = "ada" + "nim"
SHINGLE = 8  # words: a passage this long shared with the corpus is copied text, not a coincidence
HEBREW_WORD = re.compile(r"[א-תְ-ׇ\"'׳״-]{2,}")


def tracked() -> list[str] | None:
    """The repository's tracked files, or None outside a git checkout (an sdist, say)."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.splitlines() or None


@unittest.skipIf(tracked() is None, "not a git checkout")
class RepositoryRulesTest(unittest.TestCase):
    def test_no_document_or_image_file_is_committed(self) -> None:
        documents = [name for name in tracked() if DOCUMENTS.search(name)]
        self.assertEqual(documents, [], "fixtures are built in code from invented text; no document files in the repository")

    def test_ported_code_is_credited_only_in_general_terms(self) -> None:
        hits = []
        for name in tracked():
            path = ROOT / name
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if re.search(NAMES, text, re.I):
                hits.append(name)
            if re.search(ORIGIN, text, re.I) and name != "NOTICE":
                hits.append(name)
        self.assertEqual(hits, [])
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(ORIGIN, notice, re.I)), 1)


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = HEBREW_WORD.findall(text)
    return {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


@unittest.skipUnless(os.environ.get("HEOCR_TEST_CORPUS") and tracked(), "opt-in; a local folder of the private corpus")
class NoCorpusTextTest(unittest.TestCase):
    """Reads the corpus's text layers and office files (no OCR), and looks for any of its 8-word
    passages in a tracked file. Reports counts only: never prints corpus text."""

    def test_no_tracked_file_shares_a_passage_with_the_corpus(self) -> None:
        import heocr

        corpus: set[tuple[str, ...]] = set()
        files = [p for p in Path(os.environ["HEOCR_TEST_CORPUS"]).rglob("*") if p.is_file()]
        for path in files:
            try:
                corpus |= _shingles(heocr.extract(path, ocr=False, convert=False).text)
            except (OSError, ValueError):
                continue
        self.assertGreater(len(corpus), 0, "no corpus text was read")
        hits = []
        for name in tracked():
            try:
                text = (ROOT / name).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if _shingles(text) & corpus:
                hits.append(name)
        self.assertEqual(hits, [], f"{len(hits)} tracked files share a {SHINGLE}-word passage with the corpus ({len(files)} corpus files)")


if __name__ == "__main__":
    unittest.main()
