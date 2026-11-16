# D3 — Tables, reading order beyond one column, and numbers checked against a second reading

- Status: planned
- Owner: `pipeline[claude/claude-code#shay-macbook-m4]@heocr/opensource`; the lead reviews and merges (heocr-engine decision 0001)
- Serves: heocr-engine decisions 0019 and 0020; proposal `document-pipeline-package.md` §3.3 (tables, columns), §3.6 (cross-checks), §1 (reversed amounts, silent loss)
- Builds on: D1 and D2 ([d2-routes.md](d2-routes.md)), and D2c's `digit_groups_suspect` flag, whose repair was left to D3
- GitHub: milestone `D3 - Tables and numbers`, label `roadmap:D3`

## Why

On the dev split at the end of D2, 1,222 of 2,219 text-layer pages (55%) are flagged `reading_order_unresolved_columns` (counts only, local):

| Kind of page | Pages | What it needs |
|---|---|---|
| Table-like: 3 or more rows with several side-by-side segments | 870 | table structure |
| Only page furniture side by side: a running header or footer and a page number | 124 | header/footer roles |
| One or two side-by-side rows in the body: forms, "label: value", signatures | about 228 | flagged as now, or table-like pairs |

The comparison that started the package found the same thing. docling reversed the word order in 84% of PDF table cells. Amounts that looked plausible were silently reversed (`000,005,8`). Silent loss is the worst failure.

## Rules that bind every slice

The D2 rules hold:
- licences;
- tuning on D-bench synthetic data and the dev split only;
- synthetic fixtures;
- aggregate figures only in PRs.

Three more apply here:
- **Cell text comes from the page.** Every cell takes its words from the text layer by position, or from OCR of its own region where the layer has none. No word is invented, and a structure detector is never a text source.
- **Ambiguity is flagged, never guessed.** That covers a table whose grid cannot be resolved, an unknown column order, or an unconfirmed number.
- **docling stays optional.** Its table detector may come in only as an optional `[layout]` extra (code MIT, layout weights Apache-2.0 or CDLA-Permissive-2.0, checked against decision 0014 before use), as a detector of regions. It is never a text source, and no default route needs it.

## Slices

Each slice is its own PR from main. None is stacked.

| Slice | Content | Depends on |
|---|---|---|
| **D3a** | **Page furniture.** Running headers, footers and page numbers: text in the top or bottom band of the page that repeats across pages (same text, or a number sequence, at the same place). Detected and given the `header` / `footer` roles, as in DOCX, so it leaves the body flow and no longer creates side-by-side "columns". Also covers repeated site menus in web-printed PDFs. | D2 |
| **D3b** | **Ruled tables in PDF.** Grid lines from vector paths: horizontal and vertical segments, and thin filled rectangles, merged into a grid. Each cell takes the text-layer characters inside it by position, and its lines are built by the existing RTL line building inside the cell. Columns are ordered right to left in an RTL table, and spanned cells are found from missing grid lines (colspan, rowspan). A grid that cannot be closed is flagged `table_grid_unresolved`, and its text stays as lines. | D2 |
| **D3c** | **Unruled tables and side-by-side text.** Table-like pages without grid lines: column bands from word alignment across 3 or more rows, accepted only when the bands are stable. A clear two- or three-column body layout is ordered by its gutters (RTL: the right column first). The rest stays flagged. | D3a, D3b |
| **D3d** | **Numbers checked against a second reading.** For number-bearing tokens in text-layer blocks (amounts, dates, percentages, thousands groups), an independent reading comes from OCR of the token's own region on the rendered page (Tesseract, psm 7 or 8). A disagreement is flagged `number_disagreement`, never resolved by a guess. **The `digit_groups` repair lives here:** a `digit_groups_suspect` token such as `000,005,8` is repaired only when OCR of its region reads exactly one of its two readings (8,500,000 or 8,005,000). It is logged as `reversed_digit_groups`, with the confirming reading recorded. Otherwise the flag stays. | D2e |
| **D3e** | **Tables in OCR'd pages.** On T4/T5 pages, the D3b grid detection runs on the rendered image's ruling lines. Each cell is OCR'd by region, so a table row cannot be dropped silently (the OCR route of the original comparison lost a row and marked the page fine). A row whose cells OCR found empty, although they hold ink, is flagged. | D3b, D2f |
| **D3f** | **Note references in DOCX.** Footnote and endnote references are marked in body blocks (`note_refs`) and rendered as `[^n]` in Markdown, so notes are tied to their place. Carried from D1d. | D2 |

**Order:** D3a and D3f first, since they are small and independent. Then D3b and D3d in parallel, then D3c and D3e.

## Measures

Evaluation measures on D-bench, at pinned commits, following the proposal's §5.4. The relevant measures:

- **Table cells:** text and position, per format and class. Docling and markitdown run as baselines, so the 84% reversed-cell finding is checked against our route.
- **Exact numbers and amounts** (each amount, date and percentage a token, exact and in logical order), plus the flag precision of `number_disagreement`.
- **Reading order** on multi-column and table pages (CER/WER in reading order, `line_fold_v1`).
- **Silent loss:** gold characters outside both the blocks and the unread regions.

On the dev split, measured by me (counts only):
- the share of pages flagged `reading_order_unresolved_columns`, from 55% now;
- the share of table-like pages read as tables;
- the number of `digit_groups_suspect` tokens confirmed, against those left flagged. The dev split has none today; set B's number-dense case does.

## Exit criteria

- **Headers and footers:** page furniture is out of the body flow on dev, and the flag rate for unresolved columns drops by at least the 124 furniture pages.
- **Ruled tables:** read as tables on D-bench synthetic cases, with cell text and position exact and columns in RTL order. Real dev table-like pages are no longer flagged unresolved where the grid closes.
- **Numbers:** each number disagreement is flagged, and a digit-group repair is made only with a confirming reading. With repairs off, the output is byte for byte the extracted text.
- **CI** is green on Linux, macOS and Windows. Tests that need Tesseract stay opt-in.

## Not in D3

- **Formats:** DOC, RTF and ODT through LibreOffice, images as input files, PPTX and XLSX, and splitting spreads (D4).
- **The VLM route** (D2g), whose wiring waits for evaluation's registration of the bake-off.
