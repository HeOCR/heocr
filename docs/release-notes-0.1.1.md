# heocr 0.1.1: release notes

0.1.1 is 0.1.0 plus the fixes below. It is superseded by 0.2.1.

## Fixed since 0.1.0

- **A number reading that starts with a slash no longer makes the record invalid** (#94).
  - The path guard took a Tesseract reading of digits such as `/2.` for a file path, and refused the whole record.
  - Number readings are now content, like block text. Every other field is still guarded.
- **Bare paper around a scanned table is no longer read as text** (#95, #98).
  - heocr reads a scan's ruled tables cell by cell, then the rest of the page once.
  - When the rest was bare grainy paper, Tesseract read the paper's texture as thousands of characters.
  - The rest is now skipped when it holds no ink. Ink, faint ink included, is looked for in small tiles, so a short faint line beside a table is still read.
- **Outline labels that two readings agree on are restored** (#99).
  - On scans, a label such as `2.2.4.` is often cut to `4.`.
  - A deeper reading that the word's crop and the whole-page reading both give is now used, unless a known neighbouring label contradicts it.
- **Tests that need the real Tesseract now run wherever it is installed**, and a release runs every optional test before it is tagged (#96, #97).

## Record

Additive, within `heocr_document.v1`:
- page flag `ocr_rest_blank`: the page's tables were read, and nothing else on it had ink;
- key `agreed` on a number reading in `checks.number_readings`: a cut label restored because two readings agree.

## Known issues

- **Pointed text in a kept OCR layer.** Some scans carry someone else's OCR layer with niqqud or cantillation, with each mark in its own box, as Internet Archive's scans do. heocr keeps such a layer, but builds the marks into separate pieces, so pointed lines come out fragmented, and many of the layer's words do not come through. Unpointed layers are not affected. There is no switch to force OCR yet: convert those pages to images and read the images, which go through OCR. A fix is planned for the next release.

## Coming next

- **Model routing (D6):** `--ocr-model auto` picks an OCR model per document, old print or modern. It's opt-in first.
- **T6:** amounts drawn as images or vector shapes inside a text layer are not yet read. They will first be flagged, then read by OCR and merged in reading order.
