# heocr 0.1.2: release notes

0.1.2 is 0.1.1 plus three changes made after it. It is superseded by 0.2.1.

## Added

- **`--force-ocr`** (`force_ocr=True`) reads every PDF page by OCR, its text layer ignored.
  - It's a workaround for any text layer heocr reads badly.
  - The page keeps its class; its route is `ocr` and it is flagged `ocr_forced`.
  - It needs OCR, so it cannot be combined with `--no-ocr`.

## Fixed

- **Pointed text in an OCR layer that gives each mark its own slot is read whole.** This was a known issue in 0.1.0 and 0.1.1.
  - Some OCR layers, Internet Archive's among them, give every character of a word, its niqqud and cantillation marks included, an equal share of the word's box. heocr built such marks into separate pieces.
  - A mark that sits on no letter now joins the letter before it in the layer's stream.
  - Unpointed layers are read as before.
  - Its effect on pointed layers is not measured on a held-out set yet.
- **A scrambled OCR layer over a table page is replaced when our reading strongly disagrees.**
  - When the layer and our confident reading share under 35% of their letters, a lower misspelling floor (10%) lets the layer be replaced.
  - It fixes one known case, not a class.

## Record

Additive, within `heocr_document.v1`: option `force_ocr` in `options`, and page flag `ocr_forced`.

## Known limitations

The 0.1.0 notes' known limitations still apply, except the pointed-text one, which this release fixes.
