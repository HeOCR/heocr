# Changelog

All notable changes to `heocr`. Numbers in brackets are pull requests in
[HeOCR/heocr](https://github.com/HeOCR/heocr). The milestones (D1–D5) are planned in
[`planning/milestones/`](planning/milestones/).

## Unreleased → 0.3.0

### Changed

- **Public releases are paused.** Install the development version from GitHub. HeOCR will publish a package or model release only after it beats existing readers on at least one document type. The GitHub install has no bundled weights and falls back to stock Tesseract `heb`, flagged `bundled_model_unavailable`. Tag-triggered package publishing has been removed.
- **Model provenance records the final deposits** for the previously distributed bundled models. The weights and their SHA-256 hashes did not change. `heocr_modern_v1`'s notice is from the v4 deposit.

### Added

- `heocr regress` runs the frozen local engineering checks for a release candidate and writes a private report.

### Fixed

- **Prepare low-resolution grayscale scans for OCR.** Page-sized continuous-tone PNG and TIFF scans with a declared low DPI use a larger working raster while retaining the source resolution and point geometry.
- **Recover corroborated blue form entries.** Sparse blue words or numbers on a mostly neutral scan can be read separately and added with their own boxes when the original OCR leaves that region uncovered.
- **Restore Hebrew prose ellipses after OCR.** A three-dot run attached to Hebrew prose can be restored to an ellipsis. The text-layer and `--no-repairs` paths preserve the original reading.
- **Recover ruled tables from camera JPEGs with broken strokes.** If ordinary grid detection fails, corroborate the grid against ink on each cell edge before attempting the existing validated cell reads.
- **Read corroborated blue checkmarks on scanned forms.** A visible tick inside a printed box is emitted with its location when companion empty outlines corroborate the form geometry.
- **Recover missed narrow prose on partially read camera JPEGs.** Add a line only when two crop reads corroborate it and its box is separate from existing text. Keep the original page reading.
- **Join reversed Hebrew line fragments on camera JPEGs.** Confirm that both fragments form one row inside a text column with two crop reads before joining their original words.
- **Recover missed text bands on scanned PDFs.** HeOCR proposes line boxes from unread pixels and adds corroborated, non-overlapping reads within an existing text track.
- **Recover text on shaded bands in partially read scans.** A crop-confirmed line can be added when its background is consistently darker than the paper and it fits an existing text track.
- **Recover narrow prose and remove spurious overlapping fragments on scanned PDFs.** Two independent crop reads corroborate each change. Narrow lines are placed in their own paragraphs to preserve native reading order.
- **Preserve corroborated cell text on scanned forms.** On grids with tall cells, a native page reading can recover cell text omitted by isolated cell OCR when a crop confirms the same words.
- **A number beside a Latin parenthetical keeps its place in RTL text-layer lines.** In a line like "משנת 2018 (Assigning responsibilities), ..." the year and the parenthetical were read as one left-to-right run, so the year came out after the Latin text, with a bracket on each side ("Assigning responsibilities) 2018)"). Where the document's brackets were mirrored, both brackets then opened the same way. The brackets now take the paragraph's direction when the number stands outside them, as the page shows it, and the pair is read "(" then ")" whatever characters the layer gives it (LibreOffice writes both as ")"). A number inside the brackets ("(Annual Report 2020)") stays in the Latin run.
- The document's bracket mirroring (`rtl_bracket_order`) never changes a block twice.
- Reported by an outside tester on an invented LibreOffice PDF, on 0.1.2 and 0.2.0rc1. Its effect is not measured on a held-out set yet.
- **Tesseract runs with a one-thread limit.** The configured limit is recorded as `tesseract:threads` in `producer.components`.
- **Unruled tables on OCR pages are returned in row order.**
  - Tesseract reads a page block by block. On a table without rulings it can read each column as a block of its own, so every cell of one column came out, then every cell of the next.
  - heocr now finds such a table from the boxes of Tesseract's words (`heocr.pdf.ocrrows`): runs of at least 3 rows of short pieces a column gap apart, whose rows Tesseract read as lines of their own, with columns that line up in every row. Its cells are given back in rows, as a table block (`unruled_table`) placed among the page's blocks by its top edge.
  - A table Tesseract already read row by row keeps its order, and so does prose in columns. A run whose columns do not line up is left as Tesseract read it.
  - A number Tesseract split over two blocks, and the number model read again whole, could come out twice; in such a table the repeat is dropped, and the block records it (`ocr.duplicate_lines_dropped`).
  - Its effect is not measured on a held-out set yet.

### Record

- On OCR pages: the block flag `unruled_table`, the page flag `unruled_tables` and the key `ocr.duplicate_lines_dropped` (additive, within v1).

## 0.3.0a2 (2026-11-20, a pre-release of 0.3.0)

Documentation only beyond 0.3.0a1: the same code, with results measured on generated documents removed from the README, the changelog and the release notes, as in 0.2.1, and the guide to consuming heocr's output (`docs/agent-guide.md`).

## 0.3.0a1 (2026-11-20, a pre-release of 0.3.0)

A pre-release from `main`, to try what comes after 0.2.0: Word output, the command-line warning for a missing OCR model, and a fix for correct OCR layers over pages of amounts, dates and codes. Its changes are not measured on a held-out set yet.

### Added

- **Word output:** `doc.to_docx()`, `heocr.write_docx(doc, path)`, and `-f docx` on the command line (or `-o name.docx`).
  - Paragraphs are right to left, with Hebrew runs marked right to left, in Arial at 12 pt.
  - Headings use Word's Heading 1–6 styles. List items keep their labels as read. Tables are Word tables, right to left when the table is, with their merged cells. Each page of a PDF starts on a new page.
  - Notes, comments, headers and footers follow the body in their own sections, as in `doc.text`. Their titles are Hebrew when most of the body is Hebrew ("הערות שוליים", "כותרות עליונות"), else English.
  - Known limitation: footnote and endnote references are not marked in the body, as in `doc.text`; the notes are listed, with their numbers, in their section.
  - The file is a plain OOXML package written with the standard library: no new dependency. heocr's own DOCX reader reads it back to the same body text.
  - Writing a Word file to a terminal is refused: give `-o` or redirect the output.
- **A missing OCR model is reported on the command line.** An install from the repository has no bundled models, so the default model falls back to stock Tesseract, flagged `bundled_model_unavailable`. The command line now prints a warning on stderr whenever that happens, even with `-q`,.

### Fixed

- **A correct OCR layer over a page of amounts, dates and codes is no longer dropped as foreign.**
  - A page under an OCR layer with little Hebrew was taken for another language's OCR (`foreign_ocr_layer`) and read again, its layer ignored. A table of numbers and codes has no Hebrew either, and its layer may well be right, so correct layers were dropped.
  - Foreign now needs Latin text: at least 10 Latin letters in words without a digit, and at least a quarter of the layer's letters and digits. A code such as `QX-418` is not a Latin word. Real foreign layers hold far more.
  - A layer with neither (`ocr_layer_numbers_and_codes`) is kept when its digits agree with a second reading of a few of its lines by the number model (`ocr_layer_kept_by_digits`). Otherwise, or without OCR, the page is read as foreign, as before. Digits never replace a layer that was kept before.

### Record

- New page flags `ocr_layer_numbers_and_codes` and `ocr_layer_kept_by_digits`, and page signals `latin_word_letters`, `latin_word_share`, `ocr_layer_digit_agreement`, `ocr_layer_digits` and `ocr_layer_digit_confidence` (all additive, within v1).

## 0.2.1 (2026-11-20)

Documentation only: the code is 0.2.0's, with the version changed. Results measured on generated documents are removed from the README, the changelog and the release notes; heocr reports results on real material only (PS-v1, NQ1 and handwriting). 0.2.1 supersedes 0.2.0 and the 0.1 releases.

## 0.2.0 (2026-11-20)

0.2.0 is the code of `0.2.0rc1` with its version and documentation updated. The default OCR model is HeOCR's own `heocr_modern_v1`.

### Note: no niqqud from the default model

The default model, `heocr_modern_v1`, does not output niqqud: on HeOCR's held-out pointed-print test (NQ1) it wrote no vowel marks, so pointed text comes out unpointed. Stock Tesseract's `heb` model recovers some of them: 24.6% of the vowel marks with tessdata_best and 32.6% with tessdata_fast, read by plain Tesseract. To read pointed text with stock Tesseract, use `--ocr-model heb` (or `ocr_model="heb"`); it reads with the `heb` model installed with your Tesseract, which is tessdata_fast in Homebrew and most Linux packages. Pointed-text support is planned.

### Held-out results (real pages only)

All figures come from real held-out sets: photographs of real printed or handwritten pages that no HeOCR model was trained, tuned or chosen on, scored once under rules fixed before the run. They were measured with heocr 0.2.0. 95% intervals are in brackets.

**Printed pages photographed by phone (PS-v1).** 12 pages of modern printed Hebrew, typeset by us, printed and photographed with three phones (154 photos).

- heocr's own model adds nothing here: +0.08 points of character error against stock Tesseract's model inside heocr [−0.74, +1.28].
- Against plain Tesseract (tessdata_best), heocr is slightly worse on flat photos from all phones: +3.6 points [+1.5, +6.3]. On the headline photos (flat, main phone) the difference is +1.0 [−0.3, +2.6], not significant.
- heocr finds more of the numbers: 76.8% against 69.9% on the headline photos.

Not a sample of real-world documents: one printer, three phones, our typesetting.

**Pointed print (niqqud) photographed by phone (NQ1).** 25 pages of modern pointed Hebrew in open fonts, 101 photos.

- heocr's default model writes no niqqud: pointed text comes out unpointed.
- Stock Tesseract (tessdata_best) recovers 24.6% of the vowel marks.
- On the letters alone, heocr matches stock Tesseract's model inside heocr: +0.6 points [−0.1, +1.3].

Modern pointed print in open fonts, not old pointed books.

**Handwriting (not supported yet).** On 225 held-out handwritten lines by 10 writers, from ivrit.ai's [Hebrew handwriting OCR benchmark](https://huggingface.co/datasets/ivrit-ai/hebrew-handwriting-ocr-benchmark) (test split), every reader gets most characters wrong: heocr 83.1% character error [79.3, 88.0], stock Tesseract 75.4% [71.0, 80.4]. No HeOCR model is trained on handwriting.

### Added

- **The default OCR model is HeOCR's modern-print model, `heocr_modern_v1`**, for every document, when `--ocr-lang` is `heb`.
  - `--ocr-model heb` (or `stock`; `ocr_model=None` in Python) reads with stock Tesseract, as 0.1 did. With another `--ocr-lang`, such as `heb+eng`, stock Tesseract reads with it.
  - `--ocr-model oldprint` reads with the old-print model, `heocr_oldprint_v1` (and `modern` names the default).
- **`--ocr-model auto`** (`ocr_model="auto"`), opt-in, picks one OCR model per document from the route table (`heocr.ocr.routes`: modern and old print).
  - When the table offers one model, there is no probe.
  - Otherwise a probe samples up to 8 lines from up to 3 OCR pages, read again by each other candidate, and switches only when one scores clearly better. The deciding page is then read again by it.
  - Every OCR block records `ocr.model_route` (mode, domain, reason, and the probe's scores).
- **Bundled models:** the models in `MODELS` gain a `domain` and a `bundled` flag, and a model shipped inside the package (`heocr/models_data/`) resolves without a download, after the download cache.
- **HeOCR's own models ship inside the wheel:** `heocr_modern_v1` (modern and office print, [10.5281/zenodo.24395097](https://doi.org/10.5281/zenodo.24395097)) and `heocr_oldprint_v1` (old and rabbinic print, [10.5281/zenodo.24395013](https://doi.org/10.5281/zenodo.24395013)), both Apache-2.0, with their notices. The default's held-out results are above; `heocr_oldprint_v1` is not measured on a held-out set yet.
  - Without the bundled files (an install from the repository), stock `heb` reads instead, and the document is flagged `bundled_model_unavailable` (with `auto`, `routed_model_unavailable`).

### Fixed

- **Table cells keep their order in right-to-left rows of numbers and codes** [#112].
  - A band of the page with no Hebrew letter took its direction from its own letters. A row of amounts and dates has none, and a single Latin code among them made the band left to right, so its cells came out in reverse. Such a band now follows the document's direction, unless half its words or more are Latin words.
  - Inside a row, a Latin code next to numbers was one left-to-right run, so the cells around it came out in visual order. A gap between cells holding digits is now a segment separator: no direction run crosses it. Latin text is never split this way.
  - Some rows still come out in order but split over several lines, where cells lie a column gap apart.
  - Its effect is not measured on a registered held-out set yet.
- **A page of short cells under an OCR layer is checked, and a wrong layer there is replaced.**
  - The check of a foreign OCR layer reads a few of its lines again, but only lines of at least 8 letters. A table of one-word cells (codes, numbers, dates) had none, so its layer was never checked and was kept by default, wrong or right.
  - Such a page's short lines are now pooled until a group reaches 8 letters (`LAYER_CHECK_POOLED`), so the check runs and a disagreeing layer is flagged.
  - Replacing such a layer is possible but off by default (`LAYER_POOLED_REPLACE`): on a pooled page where neither side has words enough to spell-check, a reading at least 85% confident that shares under 35% of the layer's letters would replace it. Its evidence is one tuned page, and evaluation's held-out check found no page for it to act on, so the page is flagged (`ocr_layer_disagrees_kept`) instead.
- **A Tesseract call that fails or times out is never silent.** A second reading of numbers, a layer check, the page-number pass or a table cell caught the failure, carried on without its reading, and left no trace, so a slow or paused machine could change the output unseen. Every failed call is now counted: its page is flagged `ocr_call_failed` (with `signals.ocr_calls_failed`), and the document is flagged and gets an `unread` entry, `ocr_call_failed`.

### Record

- New page signal `ocr_layer_check_pooled` (additive, within v1).
- New flag and unread reason `ocr_call_failed`, and page signal `ocr_calls_failed` (additive, within v1).
- New document flags `bundled_model_unavailable` and `routed_model_unavailable` (additive, within v1).

## 0.1.2

### Added

- **`--force-ocr`** (`force_ocr=True`): reads every PDF page by OCR, its text layer ignored. It's a workaround for a text layer heocr reads badly, such as the pointed OCR layers in the known issues. The page keeps its class; its route is `ocr` and it is flagged `ocr_forced`. It needs OCR, so it cannot be combined with `--no-ocr`.

### Fixed

- **Pointed text in an OCR layer that gives each mark its own slot is read whole.**
  - Some OCR layers, Internet Archive's among them, give every character of a word, its niqqud and cantillation marks included, an equal share of the word's box. A mark then sits beside its letter, not on it.
  - heocr attached each mark by position to the nearest letter, often the wrong one, and the slot it left read as a gap between words, so pointed lines came out in fragments.
  - A mark on no letter now joins the letter before it in the layer's stream, which such layers write in logical order, and that letter's box takes in the mark's slot. Marks on their letter are read as before.
  - Unpointed layers are unchanged. Its effect is not measured on a held-out set yet.

- **A scrambled OCR layer over a table page is replaced when our reading strongly disagrees.**
  - A foreign OCR layer is replaced only when our confident reading disagrees with it and the layer is misspelt on its own evidence (at least 15% of its words) and well beyond our reading.
  - A table page's layer is mostly short cells, numbers and dates, which a scrambled layer misspells less, so such a layer could stay under that floor and be kept, though the two readings shared few of their letters.
  - When they share under 35%, and our reading is clearly credible (confidence at least 80, with enough words of its own to be spell-checked), the layer's own floor is now 10%; the margin over our reading still holds.
  - It fixes one known case, not a class.

### Record

- New option `force_ocr` in `options`, and page flag `ocr_forced` (both additive, within v1).

## 0.1.1

### Fixed

- **A number reading that starts with a slash no longer makes the record invalid.**
  - The path guard checked the OCR readings in a block's `checks.number_readings` (`first`, `second`, `replaced`) as if they were provenance.
  - A reading of a digit run such as `/2.` matched the guard, and `to_dict()` raised ContractError, so the document had no record at all.
  - Those readings are now treated as content, like block text. Every other key is still guarded, including a reading's `model`.
  - Found during development.

- **A scanned page with a small table no longer reads its bare paper as text.**
  - A page's ruled tables are read cell by cell, and the rest of the page is read once with the tables taken out.
  - When that rest was bare paper, Tesseract's global threshold fell inside the paper's texture, and the page gained thousands of characters of symbols and single letters.
  - When no ink is left, not even faint ink (the same tests as for a table cell), the rest is no longer read.
  - Found during development.

- **Faint text beside a scanned table is no longer skipped as bare paper.**
  - The check that the rest of a page holds no ink measured ink as a share of the whole page. A short faint line, 43 to 65 grey levels under the paper, held fewer such pixels than that share, so the rest was skipped and its text lost.
  - Ink is now looked for in 30 pt tiles: the rest counts as bare only when no tile holds 50 pixels 25 levels or more under the paper.
  - Found during development.

- **A cut outline label that two readings agree on is restored.**
  - On a scan whose labels heb cut ("2.2.4." read as "4."), the number model's deeper reading of a low-confidence label is used only when a neighbouring label vouches for the sequence. When the neighbours are cut too, nothing vouches.
  - Now a deeper label that the word's crop and the whole-page reading both give, level for level, is used when no known label next to it contradicts the sequence. It is logged with `agreed: true`.
  - It can be turned off (`LABEL_AGREEMENT`).

### Record

- New page flag `ocr_rest_blank` (additive, within v1): the page's tables were read, and nothing else on it had ink.
- New key `agreed` (additive) on a number reading in `checks.number_readings`: a cut label restored because two readings agree.

## 0.1.0 (D5, release readiness)

0.1.0 is `0.1.0rc1` with only its version changed. Its notes are in `docs/release-notes-0.1.0.md`.

### Record

- `heocr_document.v1` is frozen. A JSON Schema ships with the package, and v1 may only change by adding (docs/contract-v1.md).
- New flags and coverage counts since D4 (all additive, within v1):
  - `table_cell_unconfirmed` (cell flag and unread reason);
  - `repeated_image_text` and `checks.repeated_image`;
  - `image_regions_repeated`, `image_regions_text_in_layer`, `image_regions_blank`, `image_regions_layer_scan`;
  - `rotated_text`, `rotated_text_read`;
  - `deskewed`, with `signals.deskew_degrees`;
  - `numbers_from_second_reading` and `checks.number_readings`;
  - `font_cp1255_shifted`;
  - `ocr_layer_disagrees_kept`, `ocr_layer_unchecked`;
  - `table_cell_garbled` and `table_cell_not_a_number` (cell flags and unread reasons), `faint_ink_read`, `number_read_at_half_resolution` [83, 85];
  - `time_limit_reached`, the unread reason `time_limit` and `options.time_limit` [88];
  - the repair rule `latin_wrap_brackets` [86];
  - in `checks.number_readings`: `label_levels`, `crops`, `chained`, `through_cut_labels` and `page_pass` [77, 80, 82, 84, 89].
- The JSON Schema and the frozen-contract rules [71]; a reference for the record, and CONTRIBUTING [72].
- `image_region_text_in_layer` and `image_region_blank` are no longer unread entries. They are no loss, and are now counted in coverage [65].

### Fixed during development

- **Fonts and layers:**
  - a font that carries Hebrew as its cp1255 byte is decoded (`cp1255_shifted_font`) [56];
  - an invisible OCR layer is checked against a second reading and replaced only when ours is the credible one [57];
  - a kept OCR layer's scan is not read again, unless the layer leaves ink uncovered [62].
- **Line building:**
  - the RTL word space after punctuation is kept;
  - two numbers apart in an RTL line are two runs (UAX #9 N1) [58];
  - a full stop with equal gaps on both sides stands alone [73];
  - a bracket between two numbers in an RTL line takes the paragraph's direction [75].
- **Brackets around Latin text:**
  - a Latin reference's brackets in RTL text are read as shown, and kept through the bracket repair [81];
  - the two brackets of a Latin parenthetical split by a line wrap are paired [86];
  - only lines valid one way and not the other vote on a document's bracket order [87].
- **Numbers on OCR pages:**
  - words with digits get a second reading by the number model [59];
  - so do number segments and split number pieces [70], and a segment's reading replaces only the words its tokens cover [74];
  - one number-model reading of each OCR page restores numbers heb cut, never adding any [80];
  - cut section labels are read whole, guarded by the label sequence [77]: candidates vouch for each other in strict sequence [82], through the cut labels between them [89], and a label read as noise is read again on a few more crops [84];
  - scanned table cells take the number model's reading more often [60], and a cell whose two readings disagree is flagged [61];
  - number cells are read at half resolution too, faint cells are read, and garbled ones flagged [83].
- **Flag precision:**
  - a repeated image is read once, and its later pages get a flagged copy [64];
  - no-loss region outcomes left `unread` [65];
  - vertical layer text is read as its own lines [66];
  - scanned table cells are flagged by uncertainty, not by a disagreement a third reading settles [85].
- **Fonts and image regions:**
  - an OCR reading a private-use code cannot stand for is not a vote [76];
  - small images that hold text are read, and region text is placed in reading order by the box its text covers [78, 79].
- **Safety:** a per-document time limit, off by default, with what is left recorded as unread (D5c) [88].
- **Layout:**
  - a long repeated line is furniture only if one of its numbers varies [63];
  - bracket labels decide a paragraph's bracket order [67];
  - skewed scans are turned straight: ruled tables from 0.3°, text from 1° [68, 69].

### Packaging and safety

- The built package is checked in CI on three systems, with a licence audit of the dependencies [54].
- Hostile and broken input is gathered in one test module. Repository rules forbid corpus text and document files [55].
- The README was rewritten. NOTICE gives a general credit for ported code [50].

## D4: every input format

- **Office files:**
  - DOC, RTF and ODT through LibreOffice to DOCX [34];
  - ODS and XLS to XLSX; ODP and PPT to PPTX [42];
  - each read by our own reader.
- **Images:** image files, each frame a page for OCR [35].
- **PDF:**
  - a spread of two pages is read as two, right page first [36];
  - a web page printed to PDF has its site menu and footer menu given the `navigation` role [38, 51].
- **HTML:** pages read with the standard library's parser [37].
- **XLSX:** each sheet a table block [39].
- **PPTX:** slides in order, with tables, notes and pictures [41].
- **PDF geometry:**
  - positions are in the page's visible box, and text outside it is unread [47];
  - a rotated page is read as displayed, unless its layer is sideways on screen [49, 52];
  - ruled tables inside image regions are read cell by cell [48].
- **Fixes:**
  - masks before OCR use the paper's grey [40, 43];
  - column and table guards [45, 46].

## D3: tables, columns and numbers

- **Page furniture** (running headers and footers) gets the `header` and `footer` roles [23].
- **DOCX** footnote and endnote references are tied to their place [24].
- **Ruled tables:**
  - read from the layer, RTL per document [25];
  - on OCR'd pages, found in the image and read cell by cell [29];
  - repairs in cells and furniture are applied and logged [30].
- **Unruled tables and running-text columns:** read, flagged as never `selection_safe` [28].
- **Numbers:**
  - read a second time by OCR, with reversed digit groups repaired only on confirmation [26];
  - table cells checked and read a second time in `eng`, keeping both readings [31, 33].

## D2: a route for every page class

- **T2** (visual order) is read through the geometric line building [9].
- **Repairs:**
  - the repair framework, with its off switch [10];
  - direction marks, `ð` per font, RTL bracket order, flagged digit groups [15];
  - symbol fonts [20].
- **T3** (broken font mapping): private-use codes are learned from the page by OCR [21].
- **OCR route:**
  - the OCR route through stock Tesseract [12];
  - images embedded in DOCX [18];
  - HeOCR's released models as an option [19].
- **Image regions** are accounted on every text-layer page; OCR that leaves ink unread is flagged [17].
- **DOCX:** `startOverride` restarts its level once [11].

## D1: the package and the contract

- **The package:** its skeleton, the `heocr` CLI and the `heocr_document.v1` contract [2].
- **Identification:** by magic bytes and container parts, never by file name [3].
- **DOCX:**
  - our own `numbering.xml` renderer with Hebrew counting [4];
  - a reader with headings, hidden text, tables and separate notes [5].
- **PDF:**
  - the T1 text-layer route with our own RTL line building [6];
  - page signals and the T1–T6 classifier [7].
