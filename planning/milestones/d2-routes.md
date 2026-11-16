# D2 — Every page class has a route: T2, repairs, OCR, coverage, the VLM route

- Status: planned
- Owner: `pipeline[claude/claude-code#shay-macbook-m4]@heocr/opensource`; the lead reviews and merges (heocr-engine decision 0001)
- Serves: heocr-engine decisions 0019 and 0020, and the proposal `document-pipeline-package.md` §§3.3–3.6
- Builds on: D1 ([d1-foundations.md](d1-foundations.md)), including its "Carried to D2" list
- GitHub: milestone `D2 - Routes`, label `roadmap:D2`

## Goal

**Every class gets a route.** After D2, every PDF page class (T1–T6) and every embedded image has a route, and the record shows which one ran:

- the text layer;
- the text layer with logged repairs;
- OCR;
- or, on request, a VLM.

**Nothing is lost silently.** Where a route is unavailable, because Tesseract is not installed or no VLM was asked for, the record says so per page or region. It never passes the page silently.

## Rules that bind every slice

- **Licences (decision 0020):**
  - The default install stays permissive.
  - Tesseract is an optional external program, called as a subprocess and never bundled.
  - kraken and VLM support are optional extras.
  - No route needs a copyleft library.
- **Models (decision 0014):**
  - Only weights with open terms, with no NC, ND or field-of-use clauses.
  - Nothing is bundled.
  - A download is explicit and pinned by sha256.
- **Repairs are not normalization** (heocr-engine decision 0002):
  - A repair restores a character the source meant, by a listed rule, logged per block.
  - With repairs off, the output is the extracted text, byte for byte.
  - Folding, spacing and orthography changes stay out of the default output.
- **Tuning (D-bench v0 plan, `heocr-restricted-eval/dbench/PLAN_v0.md`):**
  - Thresholds and repair rules are tuned on D-bench's synthetic documents and on the real corpus's `dev` split only.
  - The `test` split is scored by evaluation, never inspected for fixes.
  - D1's review fixes were made before the split existed. That was disclosed to evaluation on 2026-11-16, with the files inspected.
- **Fixtures:** all fixtures are synthetic, generated in `tests/synth/`. No corpus text appears in PRs, tests or commit messages; PRs carry aggregate figures only.

## Slices

| Slice | Content | Depends on |
|---|---|---|
| **D2a** | T2 route. A visual-order layer is read through the existing line building (D1e already reads it exactly), flagged `text_layer_visual_order`. Its signals stay, for evaluation's check of Word-made PDFs. | D1 |
| **D2b** | Repair framework. A closed rule list in `heocr.repairs`. Each rule has an id, a reason, a synthetic example, the formats it applies to, and the evidence it needs. `extract(..., repairs=True)`, CLI `--no-repairs`. Every application is logged in `block.repairs` with code points and counts. With repairs off, the output is byte for byte the extracted text. The D1 symbol-font bullet mapping (PDF and DOCX) moves into it. | D1 |
| **D2c** | Repair rules from the D1 findings and the volunteer's `scripts/clean.py`, each driven by evidence in the content, never by file names. See "Repair rules" below. | D2b |
| **D2d** | T3 route. A T3 page whose broken characters are all repaired by D2c is read, with route `text_layer+repairs`. A page with characters no rule recovers goes to the OCR route (D2e), as T4 does; recoverable parts are never guessed. | D2c, D2e |
| **D2e** | OCR route. Pages are rendered through pypdfium2 (300 dpi). Recognition is by stock Tesseract `heb` (external; TSV output, word boxes and confidences) and, with the `[ocr]` extra, kraken with HeOCR's released recognizers for the document sets they were chosen for (decision 0018). It covers T4 and T5 pages, T6 image regions, and images embedded in DOCX. The text is marked `source: ocr` with `ocr: unverified` and its confidence. Without Tesseract, the record lists `route_unavailable: tesseract`, and the core still runs. | D1 |
| **D2f** | Coverage accounting, per page: characters read from the layer, characters from OCR, and regions left unread. Region accounting: a region whose OCR text is too short for its ink (measured from the rendered image) is flagged `ocr_short_for_ink`, never passed. | D2e |
| **D2g** | VLM route, off by default (decision 0020 item 7). Opt-in: `extract(..., vlm=ModelSpec)` or `--vlm MODEL`, with a model the user supplies or downloads explicitly, pinned by sha256, under the `[vlm]` extra. It runs only on pages and regions that the other routes could not read, or left flagged, so hard pages are defined by their failure. Every word it writes is checked against the page's text layer where one exists; disagreements are flagged. Its output is marked `vlm: unverified`. The choice of model is made from open-weights models that pass decision 0014. The plan lists candidates with their licences, for the lead's approval before any code calls one. | D2e, D2f |
| **D2h** | DOCX numbering residuals: "%1" that follows a heading from another list, and an off-by-one case in one file. Each is first reproduced as a synthetic case. The model changes only where evaluation's synthetic gold (D-bench §4 b) shows which side is right. | D1 |

**Order of the PRs:** D2a, D2b and D2h can go first, in parallel. D2e comes next. D2c and D2d follow D2b. D2f follows D2e, and D2g comes last.

## Repair rules (D2c)

Each rule needs its evidence before it fires, and logs what it changed.

| Rule | Evidence that must be present | Change |
|---|---|---|
| `direction_marks` | U+200E LRM, U+200F RLM or U+061C ALM in office XML text, next to a digit, a letter or punctuation | removed (logged) |
| `latin1_eth_nun` | ð next to Hebrew letters, and no נ anywhere in the same font (cp1255 0xF0 read as Latin-1) | ð → נ |
| `font_private_use` | private-use codes inside Hebrew words, in a font whose glyph names or encoding differences (read through pdfminer.six) name the Hebrew letter | code → letter named by the glyph |
| `rtl_bracket_order` | in an RTL unit, a bracket order that is invalid as read and valid when every bracket is mirrored (the volunteer's rules 5–7) | brackets mirrored |
| `reversed_digit_groups` | a token such as `000,005,8`: a thousands group order that cannot be a number, whose reversal can | digit groups reversed |
| `symbol_font_bullet` | a private-use symbol-font code at line or cell start, before a space | → • (moved from D1) |

Codes a rule does not cover stay as they are, and the block is flagged. In particular, font codes left unresolved by the volunteer, such as U+F001, are not guessed.

## Exit criteria

On D-bench v0, measured by evaluation at a pinned commit:

- **Routes:** every case has a route. On the synthetic documents, silent loss is 0, meaning no gold character is outside both the output and an unread region.
- **T2** pages read as well as T1 pages, in CER and WER in reading order.
- **T3** synthetic cases are fully restored by their rules. With repairs off, the output is byte for byte the extracted text.
- **OCR:**
  - T4 and T5 pages go through OCR, with stock Tesseract always reported beside HeOCR's recognizers (the standing rule).
  - Without Tesseract, every OCR-needing page is recorded as `route_unavailable`.
- **VLM:**
  - off by default;
  - when on, it runs only on failed pages or regions;
  - every word is checked against the layer where one exists;
  - no model is bundled.
- **Real corpus:** the reference-free measures on the `dev` split do not get worse than at the end of D1. The `test` figures are reported by evaluation.
- **CI** is green on Linux, macOS and Windows. Tests that need Tesseract or a model are opt-in and skipped in CI.

## Not in D2

- **Reading order:** tables and unresolved columns (D3). `reading_order_unresolved_columns` was set on 3,310 real pages in D1.
- **Number cross-checks** between two independent sources (D3).
- **More formats:** DOC, RTF, ODT, PPTX, XLSX, images as input files, and splitting spreads (D4).
- **Note references** in body text (D3, with tables).
