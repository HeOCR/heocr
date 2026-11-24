# heocr: a user guide

How to install heocr, read documents with it from Python or the command line, and read what it tells you. The full record is described in [record.md](record.md), and the formats and routes in the [README](../README.md).

## Install

heocr needs Python 3.11 or later and Git. Install the development version from GitHub:

```sh
pip install git+https://github.com/HeOCR/heocr
```

HeOCR has no current public package or model release. We will publish a release only after heocr beats existing readers on at least one document type.

The GitHub install has no bundled OCR models. With Tesseract and Hebrew language data installed, the default OCR falls back to stock `heb`. The CLI warns and the record includes `bundled_model_unavailable`. The warning is printed even with `-q`. Use `--ocr-model heb` or `ocr_model="heb"` to select stock explicitly.

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

### Capture router v2

Use `heocr scan.pdf --ocr-router v2` or `heocr.extract("scan.pdf", ocr_router="v2")` to enable capture routing. PDF pages and regions that need OCR use the pinned Rivok Full v2.0 int8 model. JPEGs with camera exposure or focal-length EXIF use `heocr_modern_v1`. Inputs without a capture signal use the usual model route.

You can override classification with `--ocr-capture-type` or `ocr_capture_type=`. For example, use `heocr scan.png --ocr-router v2 --ocr-capture-type scanner` for a known scanner image. `phone`, `iphone` and `pixel` pick modern_v1, and `scanner` and `issuer-render` pick Rivok. The capture declaration applies to the whole input and also works on its own. Keep the model, language and alternate-backend options at their defaults. The OCR record includes the capture signal and chosen model. When the classifier or declaration picks a pinned model, a missing or mismatched local file is reported as unavailable.

The PDF reader still uses readable text layers. Use `--force-ocr` to read a rendered PDF page in place of its text layer. Declare phone captures wrapped in PDF to select modern_v1.

Install the optional Rivok Labs model with `heocr --download-model heb_rivok`. HeOCR checks the download before keeping it in the model cache (`HEOCR_MODELS` when set). OCR reads the local file. The router stays opt-in while carrier-matched DEV validation is pending.

See the [HeOCR component model card](../src/heocr/models_data/MODEL_CARD.heb_rivok.md) for the immutable weight pin and upstream credits. The package includes Rivok's [Apache-2.0 licence](../src/heocr/models_data/LICENSE.heb_rivok) and [NOTICE](../src/heocr/models_data/NOTICE.heb_rivok).

Add `--ocr-line-v6 /path/to/v6.traineddata` or pass `ocr_line_v6=` in Python to enable the two-model reread. With router v2, it applies to prose from Rivok page OCR, so you can pass the same option for a batch with mixed input captures. You can also pair it with `--ocr-capture-type scanner`, `--ocr-capture-type issuer-render`, or `--ocr-model heb_rivok`. The option rereads existing prose lines and preserves their geometry and order. It expects the pinned local v6 checkpoint with SHA-256 `52b94ef13eaa3c8d32403ab05e12fc9be74d22a2d1aaa4d2de904b313b0e1609`. HeOCR records the hash of the staged model and the selections on changed lines. It retains the page reading when v6 is unavailable or a reread is inconclusive. Rereading adds two recognition calls per eligible prose line.

#### CPU cost

Timing used the same ten DEV raster pages on each system, five phone photos and five issuer renders. Router v2 used declared captures and the pinned v6 reread on the five Rivok pages. The default reader used modern_v1. Standalone Rivok ran Tesseract once per raster with the same Rivok int8 weights and page segmentation mode 3. HeOCR's number rereads were disabled for the timing.

| System | Reader | Pages/min | Peak RSS (MiB) |
| --- | --- | ---: | ---: |
| Apple M4 Max, macOS 15.7.4, Tesseract 5.5.3 | HeOCR default | 37.44 | 753.88 |
| | HeOCR router v2 with v6 | 14.68 | 742.39 |
| | Standalone Rivok int8 | 42.77 | 356.45 |
| GitHub Ubuntu 24.04 runner, Xeon Platinum 8370C, Tesseract 5.3.4 | HeOCR default | 16.43 | 494.43 |
| | HeOCR router v2 with v6 | 8.11 | 493.57 |
| | Standalone Rivok int8 | 28.13 | 262.45 |

Pages/min is the median of three serial batches with Tesseract limited to one thread. Peak RSS is the maximum summed resident memory of the reader and its child processes, sampled every 20 ms. The timing includes program startup and varies with page size and the number of prose lines available for rereading.

#### Photo grayscale and Otsu

Use `--photo-preprocess gray-otsu` (Python `photo_preprocess="gray-otsu"`) to send grayscale pixels to the main Tesseract reader and explicitly select its built-in Otsu thresholding. It applies to standalone camera JPEGs recognized by EXIF, or standalone images declared as `phone`, `iphone` or `pixel`. For example, use `heocr photo.jpg --ocr-router v2 --ocr-capture-type phone --photo-preprocess gray-otsu`. The OCR options record the setting on eligible input. Page geometry, DPI, model selection and scan routes use their usual settings.


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
- `doc.to_docx()` (bytes) / `heocr.write_docx(doc, path)`: a Word file. Paragraphs are right to left, with Hebrew runs marked right to left, in Arial (which has Hebrew letters in Word, LibreOffice and Google Docs). Headings use Word's Heading 1–6 styles. List items keep their labels as read, written as text. Tables are Word tables, right to left when the table is. Each new page starts on a new page. Notes, comments, headers and footers follow the body in their own sections, as in `doc.text`, under Hebrew titles when most of the body is Hebrew. Footnote references are not marked in the body (as in `doc.text`). Figures that were not read leave no trace, as in `doc.text`.
- `doc.blocks`, `doc.pages`, `doc.unread`, `doc.flags`: the same record as Python objects.

`heocr.validate(record)` checks a record (a dict) against the contract and returns a list of errors, which is empty when it is valid.

The options, all keyword-only:

| Option | Default | |
|---|---|---|
| `ocr` | `True` | Run OCR where the text exists only as an image. `False` reports those pages unread. |
| `ocr_lang`, `ocr_psm` | `"heb"`, `3` | Tesseract's languages and page segmentation mode |
| `tesseract` | found | The Tesseract program, when it is not on the `PATH` |
| `ocr_model` | `"heocr_modern_v1"` | The GitHub install has no bundled models and falls back to stock Tesseract with `bundled_model_unavailable`. Choose `"heb"`, `"stock"` or `None` to select stock explicitly, or pass a local `.traineddata` path. |
| `force_ocr` | `False` | Read every PDF page by OCR, its text layer ignored (`--force-ocr`); flags each page `ocr_forced`. Needs `ocr` |
| `check_numbers` | `"suspect"` | The second reading of numbers (below) |
| `repairs` | `True` | `False` turns every repair rule off: the text exactly as extracted |
| `convert`, `soffice`, `convert_timeout` | `True`, found, 180 s | LibreOffice for the older office formats |
| `max_pages` | 2,000 | Pages or image frames read at most (below) |
| `time_limit` | none | Seconds for the whole document (below) |

## From the command line

```sh
heocr notice.pdf                     # text to standard output
heocr letter.docx -o letter.md       # the format follows the suffix: .md, .json, .docx, else text
heocr scan.pdf -o scan.docx          # a Word file (or -f docx > scan.docx)
heocr notice.pdf -f json             # the record
heocr scan.pdf --ocr-lang heb+eng    # Tesseract's languages
heocr notice.pdf --no-ocr            # never run OCR; pages that need it are reported unread
heocr book.pdf --force-ocr           # read every page by OCR, ignoring its text layer
heocr --help                         # every option
```

The CLI prints each `unread` entry on standard error, and `-q` silences them. It exits with 1 only when the file cannot be opened or an option is invalid. A document read only in part, or not at all, exits with 0, and its `unread` entries say why.

## Experimental CPU line recognition

From a checkout, install the optional runtime with `pip install '.[crnn]'`. Supply a compatible
local ONNX model and its SHA-256 digest. The optional runtime and its dependencies have their own licence terms.

```sh
heocr scan.pdf -f json --ocr-model heb --crnn-model reader.onnx --crnn-sha256 "$READER_SHA256"
```

In Python, use `heocr.extract(path, crnn_model=model, crnn_sha256=digest)`. Tesseract provides
page segmentation, line boxes and reading order. The CRNN reads those printed Hebrew prose lines
from the working raster. Tesseract reads table cells, separately recovered form entries and
embedded image regions. Text-layer pages follow the usual routing rules. CRNN blocks record the
engine, the model checksum and unverified status. Missing readings from the model are flagged.

Models use an ONNX float32 input of shape `[1, 1, 32, width]` and logits of shape
`[1, width/4, classes]`, with dynamic width and time. Each grayscale crop is resized
proportionally to 32 pixels high, with width rounded up to a multiple of four and capped at
2,048. Input pixels use `1 - pixel/255`. Set ONNX metadata `heocr.crnn.schema=heocr.crnn.line.v1`
and `heocr.crnn.alphabet` to a JSON array of unique single-character strings in visual-order
CTC class order after the blank class at index zero. HeOCR decodes to a logical Hebrew line.

## Experimental handwriting OCR

Install the optional Kraken 7.2 runtime with `pip install '.[handwriting]'` from the repository. Use local, authorized model weights and their SHA-256 pins. HeOCR reads them offline. The optional runtime and its dependencies have their own licence terms.

For a scan already known to be handwritten, use the bundled, checksum-verified Kraken segmenter with a local recognizer:

```sh
heocr manuscript.pdf -f json --handwriting recognizer.safetensors \
  --handwriting-sha256 "$RECOGNIZER_SHA256" --handwriting-mode force
```

Automatic mode defaults to type tags. Pass `--handwriting-segmenter MODEL.safetensors` and `--handwriting-segmenter-sha256 HEX`. Every segmented text line must carry the `handwriting` or `handwritten` type tag for the page to enter the Kraken route. Mixed and untyped pages take ordinary OCR with this detector.

To opt into the experimental geometry detector, pass `--handwriting-detector geometry` with the recognizer and its SHA-256. It checks whole-page stroke widths, baseline regularity, and component shapes before loading Kraken. Pages that fail the gate take ordinary OCR. This detector and force mode can use the bundled segmenter. The geometry gate applies to image-only pages and image frames. Its behavior on mixed handwriting and print is unverified.

In Python, pass `handwriting=heocr.HandwritingConfig(model, sha256, mode="force")` to `heocr.extract`. To use the tags detector, provide `segmenter` and `segmenter_sha256` to the automatic config. To use the geometry detector, specify `detector="geometry"`. All forms require OCR to be enabled.

Successful lines are emitted in Kraken's reading order as `source: ocr` and `ocr.engine: kraken`, with page-point boxes and unverified confidence. Kraken and both model digests appear in the record. Wide images receive white padding below the original pixels to bound the segmenter's internal width. Boxes are clipped to the original image. The `handwriting_*` flags record the route or its fallback. Backend failure, an empty or incomplete reading, and padding beyond the pixel budget fall back to ordinary OCR. Ink outside recognized lines can mark a successful reading partial.

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
