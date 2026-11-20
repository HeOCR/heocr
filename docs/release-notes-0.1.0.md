# heocr 0.1.0: release notes

> 0.1.0 is the release candidate `0.1.0rc1` (commit `57e58de`) with only its version changed. The fixes since are in the [0.1.1 notes](release-notes-0.1.1.md). It is superseded by 0.2.1.

## What heocr does

`heocr` gets Hebrew text out of documents. It looks at each file and works out what it is: its format, from the content (never from the file name), and for a PDF, the state of each page's text layer. Then it sends each part to the route that fits:
- **the PDF text layer,** where it can be trusted, with heocr's own right-to-left line building;
- **heocr's own readers** for office files and web pages;
- **OCR,** through stock Tesseract, where the text exists only as an image.

Every decision is in the output record (`heocr_document.v1`): which route read each block, with which tool, and which flags say where the reading is a guess. Text that could not be read is reported in `unread`, with a reason, never dropped silently.

The core is Apache-2.0 and installs only permissive dependencies: pypdfium2, Pillow and defusedxml.

## Formats and page classes

| Input | Route |
|---|---|
| PDF | Page by page, by class (below) |
| DOCX | heocr's own reader: list numbering, notes tied to their place, hidden text dropped and flagged, embedded images by OCR |
| DOC, RTF, ODT | LibreOffice converts to DOCX (optional), then the DOCX reader |
| XLSX | heocr's own reader, a table per sheet |
| ODS, XLS | LibreOffice converts to XLSX (optional), then the XLSX reader |
| PPTX | heocr's own reader: slides, tables, notes, pictures by OCR |
| ODP, PPT | LibreOffice converts to PPTX (optional), then the PPTX reader |
| HTML | The standard library's parser; menus and site footers get the `navigation` role |
| Images (PNG, JPEG, TIFF, GIF, BMP, JPEG 2000, WebP) | OCR, each frame a page |

| PDF class | What the page is | Route |
|---|---|---|
| T1 | A good text layer | The text layer |
| T2 | A text layer in visual order | The text layer, reordered (or OCR when words are drawn reversed) |
| T3 | A broken font mapping | Repairs; OCR for the codes left |
| T4 | Someone else's OCR layer | Ignored; OCR |
| T5 | An image with no text layer (a scan) | OCR |
| T6 | A text layer with significant images | The text layer, plus OCR of the image regions |

## Known limitations

- **Pointed text in a kept OCR layer.** Some scans carry someone else's OCR layer with niqqud or cantillation, with each mark in its own box, as Internet Archive's scans do. heocr keeps such a layer, but builds the marks into separate pieces, so pointed lines come out fragmented, and many of the layer's words do not come through. Unpointed layers are not affected. There is no switch to force OCR yet: convert those pages to images and read the images, which go through OCR. A fix is planned for the next release.
- **Phone photos of pages.** A photo taken with a phone adds perspective, uneven light and curved lines. heocr straightens skew, but it does not correct perspective, and its accuracy on such photos has not been measured. A phone-scan evaluation set is being collected for a later release.
- **Latin references in wrapped lines and cells.** A Latin parenthetical that a line wrap or a cell edge splits over two lines has its two brackets paired (`latin_wrap_brackets`) only when the evidence is clear. heocr leaves the rest as read rather than guess:
  - a bracket whose partner could also be another bracket on its line;
  - Latin text outside the brackets;
  - a bracket the producer drew on the wrong side of the Latin run, which would need moving, not just mirroring.
- **Hebrew parentheticals beside Latin text.** A document decides once whether its brackets are mirrored, from the lines that read right one way only. Some cases can stay mirrored:
  - a lone bracket that closes a Hebrew parenthetical right after a Latin word at a line's end;
  - a document whose producer mixed both mappings in one line.
- **Cut list labels on OCR pages.** Tesseract's `heb` model often keeps only the last levels of a multi-level label at a line's start ("2.2." of "1.2.2."). heocr reads such labels again with the number model, and uses a reading only when the label sequence vouches for it. Some labels stay cut.
- **OCR text is always `unverified`.** OCR blocks carry their engine, settings and confidence, and numbers read by the second model are logged with both readings.
- **Not detected:** a partial OCR layer that leaves out only a line or two of a full page (see the README's limits).
- **Not read** (each reported in `unread`):
  - text drawn at an angle other than horizontal or vertical;
  - web pages filled in by script;
  - charts, SmartArt and embedded objects.
- **Handwriting** is not a target of this release: OCR runs stock Tesseract models, or a HeOCR model on request.

## Optional tools

The core runs without them. Without them, the parts that need them are reported in `unread`, never guessed.

- **Tesseract** (Apache-2.0), for OCR, with the `heb` model, and `eng` for the second reading of numbers.
- **LibreOffice** (MPL-2.0), for DOC, RTF, ODT, ODS, XLS, ODP and PPT. It runs with a throwaway profile and a time limit.

Neither is bundled. heocr records the version and executable hash of each one it uses in `producer.components`. Installation per system is in the [user guide](guide.md).

## Reporting issues

Open an issue at [github.com/HeOCR/heocr/issues](https://github.com/HeOCR/heocr/issues). Please include:
- `heocr --version`, and your Python version and system;
- the record's `producer.components`, `options`, the relevant `flags` and `unread` entries, and the page's `class` and `route`;
- what you expected and what you got.

**Do not attach or paste real documents or their text.** Describe the problem, or build a small invented document that shows it: invented text in the same layout, font or producer. Documents with personal or confidential content must never reach an issue. See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Licence

Apache-2.0. See [NOTICE](../NOTICE) and [THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md).
