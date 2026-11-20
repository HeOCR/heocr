# heocr 0.2.0

0.2.0 makes HeOCR's own modern-print model, `heocr_modern_v1`, the default OCR model. It ships inside the package. The code is `0.2.0rc1` (commit `9d99d5e`) with only its version and documentation changed. It is superseded by 0.2.1, which reports results on real material only.

```sh
pip install heocr==0.2.0
```

**Read this first:** the default model writes no niqqud. For pointed text, use `--ocr-model heb` (see the note below).

## Held-out results (real material only)

All figures come from held-out sets: material no HeOCR model was trained, tuned or chosen on, scored once under rules fixed before the run. 95% intervals are in brackets.

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

**Handwriting is not supported yet.** On ivrit.ai's held-out handwriting lines (225 lines, 10 writers), every reader gets most characters wrong: heocr 83.1% character error [79.3, 88.0], stock Tesseract 75.4% [71.0, 80.4]. No HeOCR model is trained on handwriting. The lines are from ivrit.ai's [Hebrew handwriting OCR benchmark](https://huggingface.co/datasets/ivrit-ai/hebrew-handwriting-ocr-benchmark).

## Note: pointed text (niqqud)

The default model, `heocr_modern_v1`, does not output niqqud: on HeOCR's held-out pointed-print test (NQ1) it wrote no vowel marks, so pointed text comes out unpointed. Stock Tesseract's `heb` model recovers some of them: 24.6% of the vowel marks with tessdata_best and 32.6% with tessdata_fast, read by plain Tesseract. To read pointed text with stock Tesseract, use `--ocr-model heb` (or `ocr_model="heb"`); it reads with the `heb` model installed with your Tesseract, which is tessdata_fast in Homebrew and most Linux packages. For example:

```sh
heocr poem.jpg --ocr-model heb
```

```python
doc = heocr.extract("poem.jpg", ocr_model="heb")
```

Pointed-text support is planned.

## Changes since 0.1.2

See the [changelog](https://github.com/HeOCR/heocr/blob/main/CHANGELOG.md#020-2026-11-20).
