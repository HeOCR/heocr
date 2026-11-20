# heocr

**The best open pipeline for getting Hebrew text out of any document.**

`heocr` looks at a document and works out what it is: its format, and for a PDF the state of each page's text layer. It then sends each part to the right tool:
- the text layer, where it can be trusted;
- our own readers for office files and web pages;
- OCR, where the text exists only as an image.

Every decision is recorded in the output. Text that could not be read is reported in `unread`, never dropped silently.

> Status: **0.2.0**. The default OCR model is HeOCR's own `heocr_modern_v1`, and the code is the `0.2.0rc1` that RC2's registered gate measured. The [held-out results](#held-out-results-for-020) are below, all three together. **Note: the default model writes no niqqud**, so pointed text comes out unpointed; see [Pointed text](#pointed-text-niqqud) for the stock-Tesseract option. Changes since 0.1.2 are in the [changelog](CHANGELOG.md). This branch, `main`, also holds changes for 0.3.0 that are not released yet (listed there too).

## Quickstart

```sh
pip install heocr        # the core needs only defusedxml, pypdfium2 and Pillow
```

From a clone of the repository, run `python scripts/fetch_models.py` before `pip install .`, or you get stock Tesseract instead of HeOCR's own OCR models: the repository does not hold the model files. heocr then warns on the command line and flags the record `bundled_model_unavailable`.

```python
import heocr

doc = heocr.extract("tender.pdf")   # a path or the file's bytes; any format below
doc.text        # plain text in reading order; notes, headers, footers and navigation follow the body
                # (a table is one line per row, its cells joined by " | ", in reading order)
doc.markdown    # headings, numbered sections and tables kept
doc.to_json()   # the heocr_document.v1 record: every block with its route, tool and flags
heocr.write_docx(doc, "tender.docx")  # a Word file: right to left, headings, lists, tables, page breaks
doc.unread      # what was not read, and why
```

```sh
heocr tender.pdf                      # text to standard output
heocr tender.docx -o tender.md        # the format follows the suffix: .md, .json, .docx, else text
heocr scan.pdf -o scan.docx           # a Word file, for reading and editing in Word
heocr scan.pdf -f json --ocr-lang heb+eng
heocr report.pdf --no-ocr             # never run OCR; pages that need it are reported unread
heocr report.pdf --force-ocr          # read every page by OCR, ignoring its text layer
```

The CLI prints each `unread` entry on stderr (`-q` silences them). It exits with 1 only when the file cannot be opened or an option is invalid. A document read only in part, or not at all, exits with 0, and its `unread` entries say why.

## Formats and routes

The format is decided from the content, never from the file name. The file name is never recorded.

| Input | Route | Notes |
|---|---|---|
| **PDF**, page by page | The text layer, read with our own right-to-left line building, ruled and unruled tables, page furniture (running headers and footers) and repairs. | Each page gets a class from its signals; see the class table below. |
| | Region OCR for images on text-layer pages. | Ruled tables inside such images are read cell by cell. |
| | Whole-page OCR for scans. | Ruled tables are read cell by cell; amounts in cells get a second reading by `eng`. |
| | Spreads (two pages side by side) are read as two, the right half first in RTL. | A web page printed from a browser has its site menu given the `navigation` role. |
| **DOCX** | Our own WordprocessingML reader. | List numbering rendered, foot/endnotes tied to their place, hidden and white text dropped and flagged, embedded images OCR'd. |
| **DOC, RTF, ODT** | LibreOffice converts to DOCX (optional, see below), then the DOCX reader. | |
| **XLSX** | Our own reader: each sheet is a table of the rows and columns that hold a value. | Cached values only, never recalculated; numbers and dates as their format shows them; hidden sheets, rows and columns read and flagged. |
| **ODS, XLS** | LibreOffice converts to XLSX, then the XLSX reader. | |
| **PPTX** | Our own reader: slides in order, shapes in reading order by position, tables, speaker notes (role `notes`), pictures by OCR. | |
| **ODP, PPT** | LibreOffice converts to PPTX, then the PPTX reader. | |
| **HTML** | The standard library's parser. | Encoding from the BOM, `<meta>` or windows-1255. Menus, page headers and footers get the role `navigation`. A page filled in by script is reported unread. Legislation's section numbers and margin headings are kept with their sections. |
| **Images** (PNG, JPEG, TIFF with all frames, GIF, BMP, JPEG 2000, WebP) | OCR, each frame a page. | DPI from the file, else 300 (flagged). |

PDF page classes:

| Class | What the page is | Route |
|---|---|---|
| T1 | A good text layer | Text layer |
| T2 | A text layer in visual order | Text layer, reordered (or OCR when words are drawn reversed) |
| T3 | A broken font mapping | Repairs; OCR for the codes left |
| T4 | Someone else's OCR layer | Ignored; OCR |
| T5 | An image with no text layer (a scan) | OCR |
| T6 | A text layer plus text drawn as images | Text layer, plus region OCR |

## Held-out results for 0.2.0

0.2.0 is the code of `0.2.0rc1`, the release candidate these tests measured. Read the three together.

All figures come from held-out sets: material no HeOCR model was trained, tuned or chosen on, scored once under rules fixed before the run. 95% intervals are in brackets.

**Generated documents (the release gate).** heocr 0.2.0rc1 was scored on a fresh set of generated Word and PDF documents, made after its code was frozen. It is supported in 9 of 10 document types. The tenth, PDFs with a text layer plus text drawn as images (T6), is held back by the real-document check: on real tender pages heocr reversed 0.454% of amounts, above stock Tesseract's interval (≤ 0.272%). Inside the same pipeline, heocr's own model (heocr_modern_v1), compared with stock Tesseract's Hebrew model:

- scans with a wrong OCR text layer (T4): character error 1.26% vs 3.27%, numbers missed 5.2% vs 22.7%;
- image-only scans (T5): character error 2.83% vs 4.30%, numbers missed 4.5% vs 14.9%;
- scanned tables: character error 0.38% vs 1.43%.

These documents come from the same generators heocr is developed against, so they favour it; the photo results below are the counterweight.

**Printed pages photographed by phone (PS-v1).** 12 pages of modern printed Hebrew, typeset by us, printed and photographed with three phones (154 photos).

- heocr's own model adds nothing here: +0.08 points of character error against stock Tesseract's model inside heocr [−0.74, +1.28].
- Against plain Tesseract (tessdata_best), heocr is slightly worse on flat photos from all phones: +3.6 points [+1.5, +6.3]. On the headline photos (flat, main phone) the difference is +1.0 [−0.3, +2.6], not significant.
- heocr finds more of the numbers: 76.8% against 69.9% on the headline photos.

Not a sample of real-world documents: one printer, three phones, our typesetting.

**Pointed print (niqqud) photographed by phone (NQ1).** 25 pages of modern pointed Hebrew in open fonts, 101 photos.

- heocr's default model writes no niqqud: pointed text comes out unpointed.
- Stock Tesseract (tessdata_best) recovers 24.6% of the vowel marks.
- On the letters alone, heocr matches stock Tesseract's model inside heocr: +0.6 points [−0.1, +1.3].

If you need niqqud, use `ocr_model` with stock `heb` for now. Modern pointed print in open fonts, not old pointed books.

Verdicts per document type (the release gate, aggregates only):

| Cell | Verdict |
|---|---|
| DOCX | Supported |
| DOC | Supported |
| PDF T1 (good text layers) | Supported |
| PDF T2 (text layers in visual order) | Supported |
| PDF T3 (broken font mappings) | Supported |
| PDF T4 (scans with a wrong OCR text layer) | Supported |
| PDF T5 (image-only scans) | Supported |
| PDF spread (two pages side by side) | Supported |
| Scanned tables | Supported |
| PDF T6 (a text layer plus text drawn as images) | Not supported: held back by the real-document check |

"Not supported" means no claim is made for that type. heocr still reads those pages, and its record still flags what it could not read.

## Pointed text (niqqud)

The default model, `heocr_modern_v1`, does not output niqqud: on HeOCR's held-out pointed-print test (NQ1) it wrote no vowel marks, so pointed text comes out unpointed. Stock Tesseract's `heb` model recovers some of them: 24.6% of the vowel marks with tessdata_best and 32.6% with tessdata_fast, read by plain Tesseract. To read pointed text with stock Tesseract, use `--ocr-model heb` (or `ocr_model="heb"`); it reads with the `heb` model installed with your Tesseract, which is tessdata_fast in Homebrew and most Linux packages. For example:

```sh
heocr poem.jpg --ocr-model heb
```

```python
doc = heocr.extract("poem.jpg", ocr_model="heb")
```

Pointed-text support is planned.

## What the record tells you

- **Every block** carries its `source` (`text_layer`, `office_xml`, `converted_docx`/`xlsx`/`pptx`, `html` or `ocr`) and its `tool`. OCR blocks are `unverified`, with their engine, settings and confidence.
- **Roles:** body text is `body`. `header`, `footer`, `footnote`, `endnote`, `comment`, `textbox`, `navigation` and `notes` are kept in the record and out of the body flow.
- **Repairs** fix characters the source meant but its font or producer broke, such as reversed brackets, `ð` for `נ`, symbol-font bullets, private-use codes and reversed digit groups. Each one fires only on evidence, and is logged with its counts. `--no-repairs` turns them all off: the output is then the extracted text, byte for byte.
- **Flags** say where the reading is a guess. For example:
  - `reading_order_unresolved_columns`: side-by-side text whose order is unknown;
  - `unruled_table`: a table inferred from layout alone, so its page is never `selection_safe`;
  - `ocr_short_for_ink`: ink that OCR left unread.

### `unread`: what was not read, and why

Every entry has a scope (`document`, `page` or `block`), a reason, and where possible the page and a box. The main reasons:

| Reason | When |
|---|---|
| `format_not_supported` | A format with no route yet (EPUB, email, archives) |
| `converter_unavailable`, `conversion_failed`, `conversion_timeout` | LibreOffice missing, refusing the file, or stopped at its time limit |
| `ocr_route_unavailable`, `ocr_failed`, `ocr_found_no_text`, `ocr_short_for_ink` | Tesseract missing or failing, or a page whose ink OCR did not read |
| `image_region_*`, `image_*` | Image regions and embedded images: not read, too small, or low confidence. A region whose text is already in the layer, or that is blank, is no loss: it is counted in the page's coverage (`image_regions_text_in_layer`, `image_regions_blank`), not here |
| `table_row_ink_unread` | A table row with ink in its cells that OCR read none of |
| `table_cell_unconfirmed` | A scanned table cell whose two readings (`heb` and the number model) differ, neither confident; both are kept in the cell |
| `rotated_text_not_read` | Characters drawn at an angle other than vertical. Vertical text (90 or 270 degrees: a margin note, a stamp's caption) is read as its own lines after the page's body, flagged `rotated_text` |
| `text_outside_page` | Characters drawn outside the page's visible box |
| `html_rendered_by_script` | A web page whose content is filled in by JavaScript; print it to PDF from a browser |
| `page_limit`, `time_limit`, `sheet_truncated`, `html_truncated`, `merges_not_applied`, `image_too_large` | A limit was reached (below) |
| `docx_unreadable`, `xlsx_unreadable`, `pptx_unreadable`, `html_unreadable`, `image_unreadable` | A file that does not parse |
| `chart_not_read`, `smartart_not_read`, `xlsx_drawing_not_read`, `embedded_content_not_read` | Charts, drawings, frames and embedded objects |

## Limits

Each limit, when reached, gives an `unread` entry or a flag:

- **pages or image frames:** 2,000 (`max_pages`);
- **time per document:** none by default; `time_limit` (seconds, `--time-limit`) is a safety net, checked before each page, frame and embedded image, and the step under way finishes;
- **input size:** 512 MB;
- **LibreOffice:** 180 s per conversion (`convert_timeout`); the conversion's processes are stopped;
- **HTML:** 64M characters, 2M elements, nesting depth 100 (flattened, with no text lost);
- **XLSX:** 2M cells per sheet;
- **PPTX:** group nesting depth 100;
- **cell spans:** 1,000;
- **renders for OCR:** 300 dpi, capped at 40M pixels.

What is not detected:

- **Pointed text in a kept OCR layer (0.1.0 and 0.1.1 only).** Some scans carry someone else's OCR layer with niqqud or cantillation, each mark in its own box, as Internet Archive's scans do. 0.1.0 and 0.1.1 keep such a layer but build its marks into separate pieces, so pointed lines come out fragmented. Fixed in 0.1.2. With an older version, convert those pages to images and read the images, which go through OCR.
- **A partial OCR layer:** a scanned page under someone else's OCR layer that heocr keeps is flagged `ocr_short_for_ink` when at least 10% of the scan's ink lies away from the layer's lines and rulings. A line or two missing from a full page's layer stays under that, and is not detected.

Read once:

- **A repeated image:** an image drawn again in the same document (a letterhead, a logo, a stamp) is read by region OCR where it first appears. Each later page gets a copy of that reading in its own box, flagged `repeated_image_text` and pointing to the first page (`checks.repeated_image`), with no OCR: every page keeps its text, as running headers do. A first reading that was empty or unsure repeats as counts (`image_regions_repeated`), not as new `unread` entries.

## Optional tools

The core runs without them. Without them, the parts that need them are reported in `unread`, never guessed.

- **Tesseract** (Apache-2.0), for OCR, with the `heb` model (and `eng` for the second reading of numbers). heocr finds it on the `PATH`, in `$HEOCR_TESSERACT`, or through `tesseract=` / `--tesseract`. Other options:
  - `ocr_lang` / `--ocr-lang` for the languages (default `heb`);
  - `ocr_model` / `--ocr-model` for the OCR model. The default is HeOCR's own modern-print model, `heocr_modern_v1`, which ships inside the package. `oldprint` reads with the old-print model, `heocr_oldprint_v1`, and `auto` picks one of the two per document by a short probe, recorded in each OCR block's `ocr.model_route`. The default's held-out results are [above](#held-out-results-for-020); `oldprint` and `auto` are not measured on a held-out set yet. Neither of HeOCR's models reads niqqud ([Pointed text](#pointed-text-niqqud)). `--ocr-model heb` (or `stock`) reads with stock Tesseract, as heocr 0.1 did. Another released model's name or a `.traineddata` path reads with that model; models downloaded on purpose are kept in `$HEOCR_MODELS`.
- **LibreOffice** (MPL-2.0), for DOC, RTF, ODT, ODS, XLS, ODP and PPT. heocr finds it on the `PATH`, in `$HEOCR_SOFFICE`, or through `soffice=` / `--soffice`; on macOS also in `/Applications`. It runs with a throwaway profile and a time limit. `convert=False` / `--no-convert` turns conversion off.

Neither is bundled. heocr records the version and executable hash of each one it uses in the record's `producer.components`.

## Licence and credits

Apache-2.0. The core depends only on permissive libraries: pypdfium2, Pillow and defusedxml. Copyleft tools are never in a default route. See [NOTICE](NOTICE), and [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the audit of the dependencies and the libraries they bundle.

## Project

`heocr` is part of [HeOCR](https://github.com/HeOCR), an open Hebrew OCR project. Its plan is in [`planning/`](planning/). The project-wide decisions behind it (0019, 0020) are in [heocr-engine](https://github.com/HeOCR/heocr-engine/tree/main/planning/decisions).
