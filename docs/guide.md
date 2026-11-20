# heocr: a user guide

How to install heocr, read documents with it from Python or the command line, and read what it tells you. The full record is described in [record.md](record.md), and the formats and routes in the [README](../README.md).

## Install

heocr needs Python 3.11 or later. Install the latest release from PyPI:

```sh
pip install heocr
```

Or, for the unreleased changes on `main`, install it from the repository:

```sh
pip install "git+https://github.com/HeOCR/heocr"
```

An install from the repository has no bundled OCR models (they ship in the released package only). The default model then falls back to stock Tesseract `heb`, and the document is flagged `bundled_model_unavailable`. In a clone, `python scripts/fetch_models.py` before `pip install .` fetches them and checks their pinned hashes.

The core needs only pypdfium2, Pillow and defusedxml. It reads text-layer PDFs, DOCX, XLSX, PPTX and HTML on its own. Two optional programs add the rest:
- **Tesseract,** for scans, images and image regions;
- **LibreOffice,** for DOC, RTF, ODT, ODS, XLS, ODP and PPT.

Without them, heocr still runs: the parts that need them are reported in `unread` (`ocr_route_unavailable`, `converter_unavailable`), never guessed.

### Tesseract

heocr needs Tesseract's `heb` model, and `eng` for the second reading of numbers.

**macOS (Homebrew):**

```sh
brew install tesseract tesseract-lang
```

**Debian and Ubuntu:**

```sh
sudo apt install tesseract-ocr tesseract-ocr-heb tesseract-ocr-eng
```

**Windows:** install Tesseract from the [UB Mannheim builds](https://github.com/UB-Mannheim/tesseract/wiki), and tick Hebrew under the additional languages. If `tesseract.exe` is not on the `PATH`, point heocr at it with the environment variable `HEOCR_TESSERACT`, or with `tesseract=` / `--tesseract`.

**Which `heb` model:** system packages often ship `tessdata_fast`'s `heb`. To use `tessdata_best`'s instead, put it in a folder of your own with `eng`, and point `TESSDATA_PREFIX` at that folder:

```sh
mkdir -p ~/tessdata
curl -L -o ~/tessdata/heb.traineddata https://github.com/tesseract-ocr/tessdata_best/raw/main/heb.traineddata
curl -L -o ~/tessdata/eng.traineddata https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata
export TESSDATA_PREFIX=~/tessdata
```

To check, run `tesseract --list-langs`: it should list `eng` and `heb`. heocr records the models it used, each with its sha256, in the record's `producer.components`.

### LibreOffice

**macOS:**

```sh
brew install --cask libreoffice
```

heocr also finds it in `/Applications`.

**Debian and Ubuntu:**

```sh
sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress
```

**Windows:** install LibreOffice from [libreoffice.org](https://www.libreoffice.org/download/). If `soffice.exe` is not on the `PATH`, set `HEOCR_SOFFICE` to its path, or pass `soffice=` / `--soffice`.

Each conversion runs with a throwaway profile and stops after 180 seconds (`convert_timeout` / `--convert-timeout`).

## From Python

The examples here use two small invented files, `notice.pdf` and `letter.docx`.

```python
import heocr

doc = heocr.extract("notice.pdf")       # a path or the file's bytes
print(doc.text)
```

```text
הודעה על פתיחת הליך
המועד להגשת הצעות הוא 15.03.2027
הסכום המרבי הוא 250,000 ש"ח
```

- `doc.text`: the plain text in reading order. Pages are separated by a form feed (`\f`). Headers, footers, notes and navigation follow the body, in their own sections.
- `doc.markdown`: headings, numbered sections and tables kept, with a comment marking each page.
- `doc.to_json()` / `doc.to_dict()`: the `heocr_document.v1` record.
- `doc.blocks`, `doc.pages`, `doc.unread`, `doc.flags`: the same record as Python objects.

`heocr.validate(record)` checks a record (a dict) against the contract and returns a list of errors, which is empty when it is valid.

The options, all keyword-only:

| Option | Default | |
|---|---|---|
| `ocr` | `True` | Run OCR where the text exists only as an image. `False` reports those pages unread. |
| `ocr_lang`, `ocr_psm` | `"heb"`, `3` | Tesseract's languages and page segmentation mode |
| `tesseract` | found | The Tesseract program, when it is not on the `PATH` |
| `ocr_model` | `"heocr_modern_v1"` | HeOCR's bundled modern-print model, for every document, when `ocr_lang` is `heb`. `"oldprint"`: the bundled old-print model. `"auto"`: one of the two per document from the route table (`heocr.ocr.routes`), recorded in each OCR block's `ocr.model_route`. `"heb"`, `"stock"` or `None`: stock Tesseract. Or another released HeOCR model's name or a `.traineddata` path, used instead of `ocr_lang`. The default's held-out results are in the [README](../README.md#held-out-results); `"oldprint"` and `"auto"` are not measured on a held-out set yet. Neither model reads niqqud: for pointed text, use `"heb"` ([why](../README.md#pointed-text-niqqud)) |
| `force_ocr` | `False` | Read every PDF page by OCR, its text layer ignored (`--force-ocr`); flags each page `ocr_forced`. Needs `ocr` |
| `check_numbers` | `"suspect"` | The second reading of numbers (below) |
| `repairs` | `True` | `False` turns every repair rule off: the text exactly as extracted |
| `convert`, `soffice`, `convert_timeout` | `True`, found, 180 s | LibreOffice for the older office formats |
| `max_pages` | 2,000 | Pages or image frames read at most (below) |
| `time_limit` | none | Seconds for the whole document (below) |

## From the command line

```sh
heocr notice.pdf                     # text to standard output
heocr letter.docx -o letter.md       # the format follows the suffix: .md, .json, else text
heocr notice.pdf -f json             # the record
heocr scan.pdf --ocr-lang heb+eng    # Tesseract's languages
heocr notice.pdf --no-ocr            # never run OCR; pages that need it are reported unread
heocr book.pdf --force-ocr           # read every page by OCR, ignoring its text layer
heocr --help                         # every option
```

The CLI prints each `unread` entry on standard error, and `-q` silences them. It exits with 1 only when the file cannot be opened or an option is invalid. A document read only in part, or not at all, exits with 0, and its `unread` entries say why.

## Reading the record

A block of `notice.pdf`, from `heocr notice.pdf -f json`:

```json
{"id": "b0", "kind": "text", "role": "body", "source": "text_layer", "tool": "heocr.pdf", "flags": [], "repairs": []}
```

Its page: `{"index": 0, "class": "T1", "route": "text_layer"}`.

**Where the text came from.**
- `source` is the route: `text_layer`, `office_xml`, `converted_docx` / `converted_xlsx` / `converted_pptx`, `html` or `ocr`.
- `tool` is what read it: `heocr.pdf`, or for OCR, the Tesseract version.
- For a PDF, each page's `class` (T1–T6) and `route` say how it was read.
- An OCR block also has `ocr`: its engine, settings, confidence and `status: "unverified"`. OCR text is never presented as verified.

**Roles.**
- `body` is the document's text.
- `header` and `footer` are running headers, footers and page numbers.
- `footnote`, `endnote` and `comment` are notes, tied to their place by `note_refs`.
- `textbox` holds shapes outside the flow, `navigation` web menus, and `notes` speaker notes.
- Only `body` and `textbox` make up the main flow of `doc.text`. The others follow it, so a reader that only wants the body can filter by role.

**Repairs** fix characters the source meant, but its font or producer broke, such as reversed brackets or `ð` for `נ`. Each one fires only on evidence. It is logged in the block's `repairs`, with the rule, the code points and the counts. `repairs=False` turns them all off.

**Flags** mark where the reading is a guess, or where heocr kept something as read because the evidence was too thin. For example:
- a one-line document with one bracketed phrase can't show whether its producer mirrored brackets, so the block is flagged `rtl_brackets_unverified` and its brackets are left as the layer holds them;
- `unruled_table`: a table inferred from layout alone;
- `ocr_short_for_ink`: ink that OCR left unread;
- `reading_order_unresolved_columns`: side-by-side text in an unknown order.

The full list is in [record.md](record.md).

**`unread`** is what was not read. Each entry has a `scope` (`document`, `page` or `block`), a `reason`, and where possible the `page` and a `bbox`. A document is only fully read when `unread` is empty. Check it before trusting the text as complete.

## Limits: `max_pages` and `time_limit`

`max_pages` (2,000 by default; `--max-pages`) bounds the pages or image frames read from one document, since each may cost an OCR run. The rest become one `unread` entry, and the document is flagged `page_limit_reached`:

```sh
heocr chapters.pdf --no-ocr --max-pages 2
```

```text
heocr: unread: page_limit (pages 3-3 of 3 not read (max_pages=2))
```

`time_limit` (none by default; `--time-limit SECONDS`) is a safety net for the whole document, conversion and OCR included. It is off by default, so the same file always gives the same record, whatever the machine's speed. When set, it is checked before each page, image frame, embedded image and page of number checks. The step under way finishes, since each Tesseract and LibreOffice call has its own timeout. What is left is never dropped:
- pages or frames not read become one `unread` entry with the reason `time_limit`;
- an embedded image not read becomes a `figure` block with its own `time_limit` entry;
- numbers left unchecked flag `numbers_not_checked`.

The document is flagged `time_limit_reached`, and the limit is recorded in `options.time_limit`.

```python
doc = heocr.extract("long-scan.pdf", time_limit=600)
if "time_limit_reached" in doc.flags:
    print([u.detail for u in doc.unread if u.reason == "time_limit"])
```

## The number checks

Numbers matter most in the documents heocr is built for, so it reads them a second time. Every second reading is logged next to the first, and a reviewer or a program can revert it.

**On text-layer PDFs,** `check_numbers` chooses which lines are read again by OCR of their own image:

| `check_numbers` | |
|---|---|
| `"suspect"` (default) | Only lines with a reversed-looking digit group (`000,005,8`): the evidence the `reversed_digit_groups` repair needs |
| `"tables"` | Also every table cell with an amount, a percentage or a date |
| `"all"` | Every line and cell with them; trustworthy disagreements are flagged |
| `False` (`--check-numbers off`) | No second reading |

A digit group is repaired only when the second reading confirms one of its two readings (`digit_groups_confirmed`). Otherwise it stays as read, flagged `digit_groups_suspect`. A block whose numbers disagree with the second reading is flagged `number_disagreement`, and one whose numbers could not all be confirmed is flagged `numbers_unconfirmed`.

**On OCR pages,** words with digits are read again by Tesseract's `eng` model, the number model, which reads digits better than `heb`. So are number segments, cut section labels, and the whole page once more. A reading replaces heb's only on fixed rules:
- the number model must be confident, or the label sequence must vouch for it;
- it never adds text that heb did not read.

Each replacement is in the block's `checks.number_readings`, with both readings, their confidences and the model, and the block is flagged `numbers_from_second_reading`. A reading that was not used is logged with `used: false`.

In scanned tables, each number cell keeps every model's reading in `readings`. A cell whose readings differ, neither confident, is flagged `table_cell_unconfirmed`. Cells that read as no number in a column of numbers, or as noise, are flagged too, and listed in `unread`.

Without the `eng` model, heb reads the numbers alone, and the document is flagged `numbers_read_with_main_model`.

## When something goes wrong

- **A page you expected text from is empty:** look at `unread` and at the page's `class` and `route`. `ocr_route_unavailable` means Tesseract or its `heb` model was not found.
- **An office file is not read:** `converter_unavailable` means LibreOffice was not found. `conversion_timeout` means it ran out of time.
- **Brackets, numbers or labels look wrong:** look at the block's `repairs`, `flags` and `checks`, and try `repairs=False` to see the text exactly as extracted.

To report a problem, see [the release notes](release-notes-0.1.0.md#reporting-issues). Never attach a real document or paste its text.
