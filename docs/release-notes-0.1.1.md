# heocr 0.1.1: release notes

0.1.1 is the release candidate that went through the registered release gate (`0.1.0rc1`, commit `57e58de`), plus the fixes made after its verdicts. **The gate's verdicts belong to `0.1.0rc1`.** These fixes were tuned on open synthetic sets and the development split only, never on the gate's held-out documents. They are not gated yet: a fresh registered gate (RC2, with a new seed) decides the next claims.

## The gate's verdicts (0.1.0rc1)

Supported: DOCX, DOC, PDF T1, T2 and T3, PDF spreads, scanned tables.

Not supported:
- **PDF T4** (pages under someone else's OCR layer): character errors above stock Tesseract's margin.
- **PDF T5** (scans): character errors just above stock Tesseract's margin; numbers missed more often than pymupdf4llm's.
- **PDF T6** (text layers with image regions): vetoed by the held-out real documents, for amounts reversed in mixed-direction text.

The [0.1.0 notes](release-notes-0.1.0.md) describe the gate, its baselines and its margins.

## Fixed since 0.1.0rc1

- **A number reading that starts with a slash no longer makes the record invalid** (#94).
  - The path guard took a Tesseract reading of digits such as `/2.` for a file path, and refused the whole record.
  - Number readings are now content, like block text. Every other field is still guarded.
  - This cost the T5 cell two documents with no output.
- **Bare paper around a scanned table is no longer read as text** (#95, #98).
  - heocr reads a scan's ruled tables cell by cell, then the rest of the page once.
  - When the rest was bare grainy paper, Tesseract read the paper's texture as thousands of characters. This decided the T4 cell.
  - The rest is now skipped when it holds no ink. Ink, faint ink included, is looked for in small tiles, so a short faint line beside a table is still read.
  - Checked on a held-out synthetic stress set: no line lost, and the skip fires only on bare pages.
- **Outline labels that two readings agree on are restored** (#99).
  - On scans, a label such as `2.2.4.` is often cut to `4.`.
  - A deeper reading that the word's crop and the whole-page reading both give is now used, unless a known neighbouring label contradicts it.
  - This is a small part of the T5 number gap. The larger part is the OCR model itself, which model routing (below) addresses.
- **Tests that need the real Tesseract now run wherever it is installed**, and a release runs every optional test before it is tagged (#96, #97).

## Record

Additive, within `heocr_document.v1`:
- page flag `ocr_rest_blank`: the page's tables were read, and nothing else on it had ink;
- key `agreed` on a number reading in `checks.number_readings`: a cut label restored because two readings agree.

## Known issues

- **Pointed text in a kept OCR layer.** Some scans carry someone else's OCR layer with niqqud or cantillation, with each mark in its own box, as Internet Archive's scans do. heocr keeps such a layer, but builds the marks into separate pieces, so pointed lines come out fragmented: on the pointed pages we tested, a median of under half of the layer's words came through. Unpointed layers are not affected. There is no switch to force OCR yet: convert those pages to images and read the images, which go through OCR. A fix is planned for the next release.

## Coming next

- **Model routing (D6):** `--ocr-model auto` picks an OCR model per document, old print or modern. It's opt-in first. HeOCR's own models become the default only after evaluation's numbers and a registered gate ([proposal](../planning/proposals/d6-model-routing.md)).
- **T6:** amounts drawn as images or vector shapes inside a text layer are not yet read. They will first be flagged, then read by OCR and merged in reading order. The cause of the held-out veto is not yet reproduced on open data.
