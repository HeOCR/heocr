# D1 — Foundations: contract, CLI, file types, DOCX, T1 PDF pages

- Status: in progress
- Owner: `pipeline[claude/claude-code#shay-macbook-m4]@heocr/opensource`; reviewed and merged by the lead (heocr-engine decision 0001)
- Serves: heocr-engine decisions 0019 (top goal) and 0020 (package plan); design in heocr-engine `planning/proposals/document-pipeline-package.md` §§2–4, 6–7
- GitHub: milestone `D1 - Foundations`, label `roadmap:D1`

## Goal

`heocr.extract(path)` and the `heocr` command return a `heocr_document.v1` record for any input file. D1 reads two kinds of input well:

- **DOCX,** with automatic numbering rendered by our own code;
- **PDF pages of class T1** (a correct text layer in logical order), with right-to-left lines built by our own code from character boxes.

Every other input is identified and reported as not read, with the reason. That covers other formats, and PDF pages of classes T2–T6, which are measured, classified and flagged.

## Why these first

- **DOCX numbering:** the 132-document comparison found 0.6% correct section numbers when DOCX was read from XML, against 99% when Word rendered them. That is the clearest case for our own code (proposal §1, item 2).
- **T1 pages:** they are the commonest PDF case, and text-layer readers split words on them (6.4 per 1,000 words).
- **The page classes:** they are what lets later milestones route each page. Measuring them in D1 means no page is ever read through the wrong route without a flag.

## Slices

Each slice is one PR, with offline tests on fixtures generated in `tests/synth/`. The PRs are stacked in this order; each says which PR it builds on.

| Slice | Content | Status |
|---|---|---|
| **D1a** | Package skeleton; `heocr` CLI; the `heocr_document.v1` contract (builder, JSON schema, validator, the no-local-paths rule); `extract()` dispatch, with unsupported inputs recorded rather than raised | planned |
| **D1b** | File-type identification by magic bytes and container parts, never by extension. Ports tendex `identify()` without PyMuPDF or olefile (our own OLE2 directory reader) | planned |
| **D1c** | Our own DOCX `numbering.xml` renderer: abstract and concrete lists, nine levels, `lvlText` templates, `start`, `lvlRestart`, `isLgl`, level overrides and `startOverride`, style-linked lists, and Hebrew counting (`hebrew1`, `hebrew2`) | planned |
| **D1d** | The DOCX reader: body in order; headings from outline levels (direct and through styles) as well as heading styles; hidden and deleted text dropped; line breaks in table cells kept; tables with their direction; footnotes, endnotes, comments, headers and footers kept separate from the body; images reported as unread | planned |
| **D1e** | The PDF text-layer route for T1 pages: characters and boxes through pypdfium2, and our own RTL word and line building, with columns flagged and the `selection_safe` rule. Ports tendex `word_lines`, `ordered_words` and the `layout.py` geometry | planned |
| **D1f** | PDF page signals and the T1–T6 classifier; T2–T6 pages are classified and flagged, not read; the text-layer warning flags. Ports tendex `text_flags` | planned |

## The contract (`heocr_document.v1`)

One JSON object per input file:

- **`schema`**: the literal `heocr_document.v1`.
- **`producer`**: `heocr` and its version, plus the versions of the components used.
- **`document`**: sha256, size, detected format with its evidence, and page count where the format has pages.
- **`pages`**: for paged formats, each page's index, size, class, signals, route, coverage and flags.
- **`blocks`**: in reading order. Each block has:
  - a kind: `text`, `heading`, `list_item`, `table` or `figure`;
  - a role: `body`, `header`, `footer`, `footnote`, `endnote`, `comment` or `textbox`;
  - its text, or cells for a table;
  - the page and bbox where the format has them;
  - its source (`text_layer`, `office_xml`, later `ocr`), tool and version;
  - its repairs and flags.
- **`unread`**: what was not read and why. This includes formats without a route, pages whose class has no route in D1, images, and routes whose external program is missing.

No local path or file name appears anywhere in the record, following heocr-engine's prediction contract. The validator rejects strings that look like absolute paths.

## Text and Markdown views

`doc.text` and `doc.markdown` are views of the blocks; the JSON is the record.

- **Body first:** the body comes first, in order. Notes, comments, headers and footers follow under their own separators, never mixed into the body.
- **Numbering:** list items carry their rendered label.
- **Tables** keep their logical column order, which is first column first. In a right-to-left table that is the rightmost column.
- **Line breaks in cells:** kept as `\n` in the JSON, rendered as `<br>` in Markdown.

## PDF page classes in D1

Signals are measured on every page:

- characters in the text layer;
- the share of the page covered by images;
- the number of vector paths;
- invisible text, meaning text render mode 3;
- the share of Hebrew among letters;
- the rate of Hebrew words that start with a final letter, after our line building;
- counts of U+FFFD, private-use codes and Latin-1 letters next to Hebrew letters;
- font names.

The thresholds are provisional. They will be tuned on D-bench, and each one is a named constant with its reason. The classifier tries the classes in this order, and the first that applies wins:

| Order | Class | D1 rule (provisional) | D1 route |
|---|---|---|---|
| 1 | `empty` | No text, no images and no paths | Nothing to read |
| 2 | **T5** | No usable text layer, and images or paths present | Flagged `route_unavailable_d1` |
| 3 | **T4** | Invisible text over a page-sized image, with little Hebrew or many words that start with a final letter | Flagged |
| 4 | **T3** | U+FFFD, private-use codes, or Latin-1 letters next to Hebrew above a threshold | Flagged |
| 5 | **T2** | Many Hebrew words start with a final letter after line building | Flagged |
| 6 | **T6** | A usable text layer, plus images that cover a large share of the page without being the whole page | Flagged |
| 7 | **T1** | Otherwise | Read from the text layer |

Two cases are handled specially:

- **A Hebrew OCR layer that passes the checks** is read as T1 and flagged `text_layer_is_ocr`, since it is someone else's unverified OCR.
- **Pages that may be spreads** (wider than tall) and **rotated pages** are flagged. Splitting spreads is D4.

## Out of scope in D1

- **Later milestones:** the OCR route, repairs and the VLM route (D2); table structure in PDF (D3); DOC, RTF, ODT, PPTX and XLSX content (D4).
- **Dictionary hit rate:** D2, with the OCR route.
- **ToUnicode inspection through pdfminer.six:** D2, with the T3 repairs.
- **D-bench v0:** evaluation owns it (decision 0020, item 6). The synthetic DOCX and PDF generators in `tests/synth/` are offered to evaluation as a seed for it.

## Exit criteria

- **The public API and CLI:** `heocr.extract()` and `heocr FILE [-o OUT] [--format text|markdown|json]` work on every input. Inputs without a route return a record that says so.
- **DOCX tests:** the synthetic DOCX fixtures render every numbering case above exactly. That covers multi-level lists, restarts, overrides, `isLgl`, style-linked lists, and Hebrew counting past 10, 15, 16 and 400. Headings, hidden text, cell line breaks, RTL tables and notes all come out as specified.
- **PDF tests:** the synthetic T1 PDF fixtures read back exactly, line by line, including embedded Latin and number runs. Fixtures of T2–T6 are classified correctly, and their text is reported as unread.
- **CI** is green on Linux, macOS and Windows.
