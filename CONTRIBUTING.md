# Contributing to heocr

`heocr` is part of [HeOCR](https://github.com/HeOCR), an open Hebrew OCR project. Contributions are welcome: bug reports with a way to reproduce them, documents heocr reads badly (see "Test data" first), and pull requests.

## Licences

- **heocr is Apache-2.0.** By contributing you agree your contribution is licensed the same way.
- **The core installs only permissive dependencies** (defusedxml, pypdfium2, Pillow).
  - A copyleft or non-commercial dependency never enters the default install or a default route.
  - A tool under such a licence may only be an optional, external program that heocr finds and calls (as with Tesseract and LibreOffice), never bundled.
  - Any new dependency is added to the audit in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
- **Ported code** is credited in NOTICE.

## Test data

- **No document files are committed.**
  - Every test fixture is built in code, from invented text (`tests/synth/`).
  - A test enforces this (`tests/test_repo_rules.py`): no PDF, DOCX, image or other document file may be tracked.
- **Real documents stay out of the repository**, out of issues and out of pull request text.
  - If a real document shows a bug, describe it, or build a synthetic fixture that reproduces it.
  - Never paste its text.
- **Tuning uses development data only.** Any threshold or rule tuned on real documents is tuned on the evaluation corpus's `dev` split or on synthetic sets. The `test` split is held out for the release gate and is never inspected.

## Working on the code

```sh
python -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m unittest discover -s tests -t .
```

- **Run the suite in a fresh virtual environment** with the declared dependencies only before a pull request; that is how CI runs it.
- **With the `test` extra installed** (`jsonschema`), every record the suite produces is also checked against the record's JSON Schema.
- **The OCR tests use a fake Tesseract** (`tests/synth/fake_tesseract.py`), so the suite needs no OCR engine. Tests that need the real Tesseract or LibreOffice are opt-in and skipped otherwise.
- **A change to what heocr reads** should come with:
  - a test that fails before it;
  - numbers before and after on the synthetic sets, or on `dev` as aggregate counts only.
- **The record** (`heocr_document.v1`) changes only as [docs/contract-v1.md](docs/contract-v1.md) allows: additive fields within v1, anything else is v2. Update the schema, `contract.validate`, [docs/record.md](docs/record.md) and CHANGELOG.md together.
- **Text that could not be read is never dropped silently.** It goes into `unread` or a flag. A new limit or route says what it leaves out.

## Pull requests

- **Branches:** work on a branch, never directly on `main`. `main` is squash-merged.
- **Commit messages** are [conventional](https://www.conventionalcommits.org/): `fix(pdf): …`, `feat(ocr): …`, `docs: …`, `test: …`.
- **Review:** every pull request is reviewed and needs green CI before it is merged. Authors do not merge their own pull requests.
