# heocr 0.2.0

0.2.0 makes HeOCR's own modern-print model, `heocr_modern_v1`, the default OCR model. It ships inside the package. The code is exactly `0.2.0rc1` (commit `9d99d5e`), the release candidate that RC2's registered gate measured, with only its version and documentation changed.

```sh
pip install heocr==0.2.0
```

**Read this first:** the default model writes no niqqud. For pointed text, use `--ocr-model heb` (see the note below).

## Held-out results

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

## Note: pointed text (niqqud)

The default model, `heocr_modern_v1`, does not output niqqud: on HeOCR's held-out pointed-print test (NQ1) it wrote no vowel marks, so pointed text comes out unpointed. Stock Tesseract's `heb` model (tessdata_best) recovers 24.6% of the vowel marks there. To read pointed text with stock Tesseract:

```sh
heocr poem.jpg --ocr-model heb
```

```python
doc = heocr.extract("poem.jpg", ocr_model="heb")
```

Pointed-text support is planned.

## Changes since 0.1.2

See the [changelog](https://github.com/HeOCR/heocr/blob/main/CHANGELOG.md#020-2026-11-20).
