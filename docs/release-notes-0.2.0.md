# heocr 0.2.0

0.2.0 makes HeOCR's own modern-print model, `heocr_modern_v1`, the default OCR model. It ships inside the package. The code is exactly `0.2.0rc1` (commit `9d99d5e`), the release candidate that RC2's registered gate measured, with only its version and documentation changed.

```sh
pip install heocr==0.2.0
```

**Read this first:** the default model writes no niqqud. For pointed text, use `--ocr-model heb` (see the note below).

## Held-out results

0.2.0 is the code of `0.2.0rc1`, the release candidate that RC2's registered gate measured. The three held-out tests below were all run on that candidate. Read them together: each covers what the others don't.

**1. RC2, on generated documents** (a fresh synthetic set with exact gold, with a held-out split of real documents that can veto a cell). Per cell, heocr is compared with stock Tesseract, Docling, MarkItDown, pymupdf4llm and a production tender-extraction pipeline. **9 of 10 cells are supported:**

| Cell | Verdict | Why |
|---|---|---|
| DOCX | Supported | |
| DOC | Supported | |
| PDF T1 (good text layers) | Supported | |
| PDF T2 (text layers in visual order) | Supported | |
| PDF T3 (broken font mappings) | Supported | |
| PDF T4 (under someone else's OCR layer) | Supported | |
| PDF T5 (scans) | Supported | |
| PDF spread (two pages side by side) | Supported | |
| Scanned tables | Supported | |
| PDF T6 (text layers with image regions) | Not supported | Vetoed by the held-out real documents: amounts came out reversed in 0.454% of cases, above stock Tesseract's interval (at most 0.272%). |

What the model adds, measured on the same run: heocr against the same pipeline reading with stock Tesseract's `heb`. The intervals are two-sided 95%:
- **Scans (T5):** 1.5 points fewer character errors [1.2, 1.7], and 10.3 points fewer numbers missed.
- **Pages under someone else's OCR layer (T4):** 2.0 points fewer character errors [1.7, 2.3].
- **Scanned tables:** 1.1 points fewer character errors [0.9, 1.2].
- **Text layers and office files:** no change. They don't go through OCR.

**2. PS-v1, on real phone photos** (12 printed pages, photographed with three phones, flat and under harder conditions):
- **No gain from the model.** On flat photos, heocr and the same pipeline with stock `heb` are within noise of each other.
- **Slightly worse than plain Tesseract on flat photos.** heocr makes 3.6 points more character errors than stock `tesseract` [1.5, 6.3]. The cause is in heocr's own processing of photos, not the model, and is being worked on.
- **More numbers found:** heocr finds more of the numbers on the page than stock Tesseract does (number recall 63.7% against 59.2% on flat photos).

**3. NQ1, pointed print** (photos of pointed Hebrew print): the default model writes **no niqqud**; see [Pointed text](#note-pointed-text-niqqud) below.

"Not supported" means no claim is made for that cell. heocr still reads those pages, and its record still flags what it could not read.

## Note: pointed text (niqqud)

The default model, `heocr_modern_v1`, does not output niqqud. On HeOCR's held-out pointed-print test (NQ1) it wrote no vowel marks at all, so pointed text comes out unpointed. Stock Tesseract's `heb` model keeps only about a quarter of the marks (24% on NQ1). To read pointed text with stock Tesseract:

```sh
heocr poem.jpg --ocr-model heb
```

```python
doc = heocr.extract("poem.jpg", ocr_model="heb")
```

Pointed-text support is planned.

## Changes since 0.1.2

See the [changelog](https://github.com/HeOCR/heocr/blob/main/CHANGELOG.md#020-2026-11-20).
