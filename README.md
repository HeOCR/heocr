# heocr

**The best open pipeline for getting Hebrew text out of any document.**

`heocr` looks at a document and works out what it is: its format, and for a PDF the state of each page's text layer. It then sends each part to the right tool:
- the text layer, where it can be trusted;
- our own readers for office files and web pages;
- OCR, where the text exists only as an image.

Every decision is recorded in the output. Text that could not be read is reported in `unread`, never dropped silently.

> Status: release candidate 0.1.0rc1. Milestones D1–D4 are done, and D5 is in its release gate. Nothing is on PyPI yet: the first release, 0.1.0, waits for the D-bench release gate's verdict.

## Quickstart

```sh
pip install .            # from a clone; the core needs only defusedxml, pypdfium2 and Pillow
```

```python
import heocr

doc = heocr.extract("tender.pdf")   # a path or the file's bytes; any format below
doc.text        # plain text in reading order; notes, headers, footers and navigation follow the body
                # (a table is one line per row, its cells joined by " | ", in reading order)
doc.markdown    # headings, numbered sections and tables kept
doc.to_json()   # the heocr_document.v1 record: every block with its route, tool and flags
doc.unread      # what was not read, and why
```

```sh
heocr tender.pdf                      # text to standard output
heocr tender.docx -o tender.md        # the format follows the suffix: .md, .json, else text
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
| T6 | A text layer with significant images | Text layer, plus region OCR |

## What the release gate supports

heocr's claims come from a release gate registered before the candidate existed. Per cell, heocr is compared with stock Tesseract, Docling, MarkItDown, pymupdf4llm and a production tender-extraction pipeline, on a fresh synthetic set with exact gold. A held-out split of real documents can veto a cell. Verdicts for the release candidate `0.1.0rc1` (aggregates only):

| Cell | Verdict | Why |
|---|---|---|
| DOCX | Supported | |
| DOC | Supported | |
| PDF T1 (good text layers) | Supported | |
| PDF T2 (text layers in visual order) | Supported | |
| PDF T3 (broken font mappings) | Supported | |
| PDF spread (two pages side by side) | Supported | |
| Scanned tables | Supported | |
| PDF T4 (under someone else's OCR layer) | Not supported | Character errors above stock Tesseract's margin. Most of the excess came from one page whose bare paper was read as text: addressed in 0.1.1; not yet re-gated (RC2 decides). |
| PDF T5 (scans) | Not supported | Character errors just above stock Tesseract's margin, from two documents that produced no record; that crash is gone in 0.1.1. Numbers missed more often than pymupdf4llm, which reads scans with Tesseract's English model: addressed in part in 0.1.1. The cell itself is not yet re-gated (RC2 decides). |
| PDF T6 (text layers with image regions) | Not supported | Vetoed by the held-out real documents: amounts in mixed right-to-left and left-to-right text came out reversed more often than stock Tesseract's. |

"Not supported" means no claim is made for that cell. heocr still reads those pages, and its record still flags what it could not read. 0.1.1's fixes are not yet gated: a fresh registered gate (RC2) decides the next claims.

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
  - `--ocr-model` for a HeOCR model, kept in `$HEOCR_MODELS`.
- **LibreOffice** (MPL-2.0), for DOC, RTF, ODT, ODS, XLS, ODP and PPT. heocr finds it on the `PATH`, in `$HEOCR_SOFFICE`, or through `soffice=` / `--soffice`; on macOS also in `/Applications`. It runs with a throwaway profile and a time limit. `convert=False` / `--no-convert` turns conversion off.

Neither is bundled. heocr records the version and executable hash of each one it uses in the record's `producer.components`.

## Licence and credits

Apache-2.0. The core depends only on permissive libraries: pypdfium2, Pillow and defusedxml. Copyleft tools are never in a default route. See [NOTICE](NOTICE), and [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for the audit of the dependencies and the libraries they bundle.

## Project

`heocr` is part of [HeOCR](https://github.com/HeOCR), an open Hebrew OCR project. Its plan is in [`planning/`](planning/). The project-wide decisions behind it (0019, 0020) are in [heocr-engine](https://github.com/HeOCR/heocr-engine/tree/main/planning/decisions).
