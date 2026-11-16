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
| **D2d** | T3 route (the design the lead approved on 2026-11-16). A T3 page whose broken characters D2c's rules repair is read through the layer. Private-use codes that no rule recovers are learned from the page itself (`font_private_use_by_ocr`, `heocr.pdf.fontmap`): the words holding a code are OCR'd one by one (psm 8), and a reading is accepted only when it matches every known letter. A code needs at least 3 accepted words and 90% agreement, and passes the consistency checks: letter absent in regular form, final letters at word ends, no two codes sharing a letter. Maps are learned per font and per document. A page with codes left unresolved, or a font with more than 3 unresolved, goes to whole-page OCR. The record keeps each map and its evidence counts, with no text. | D2c, D2e |
| **D2e** | OCR route, in two PRs. **D2e1**: stock Tesseract `heb` (external; TSV output, word boxes and confidences), on pages rendered at 300 dpi through pypdfium2, for T4 and T5 pages, T2 pages drawn reversed, and T6 image regions. Each run records the Tesseract version, the executable's sha256, each traineddata's sha256, the language list and the PSM. The default is `heb` only and `--psm 3`; `heb+eng` is the user's choice. **D2e2a**: images embedded in DOCX. **D2e2b**: HeOCR's released Tesseract models (R8b, R7) as an option in the same Tesseract engine (`--ocr-model`), run from a private tessdata folder with the installation's `configs`. Each is pinned to its card's sha256 and downloaded only on purpose (`--download-model`). **The default stays stock `heb`** until evaluation shows on D-bench (pages, psm 3, case sets A and B, and real dev) that a HeOCR model beats stock on a document set; then the default switches for that set only (decisions 0018 and 0020). kraken with R3h/R4d/A1 is **deferred** (the lead's decision, 2026-11-16): those models lose to stock Tesseract on real printed pages. OCR text is marked `source: ocr`, with an `ocr` object whose status is `unverified`, plus its confidence. Without Tesseract, the record says `ocr_route_unavailable` with the reason, and the core still runs. | D1 |
| **D2f** | Coverage accounting, per page: characters read from the layer, characters from OCR, and regions left unread. Image regions are accounted on every class read through the text layer (T1, T2, T6), so a T2 page with large images also lists them (the lead's review of D2a). Region accounting: a region whose OCR text is too short for its ink (measured from the rendered image) is flagged `ocr_short_for_ink`, never passed. | D2e |
| **D2g** | VLM route, off by default (decision 0020 item 7). Opt-in: `extract(..., vlm=ModelSpec)` or `--vlm MODEL`, with a model the user supplies or downloads explicitly, pinned by sha256, under the `[vlm]` extra. It runs only on pages and regions that the other routes could not read, or left flagged, so hard pages are defined by their failure. Every word it writes is checked against the page's text layer where one exists; disagreements are flagged. Its output is marked `vlm: unverified`. The choice of model is made from open-weights models that pass decision 0014. The plan lists candidates with their licences, for the lead's approval before any code calls one. | D2e, D2f |
| **D2h** | DOCX numbering residuals: "%1" that follows a heading from another list, and an off-by-one case in one file. Each is first reproduced as a synthetic case. The model changes only where evaluation's synthetic gold (D-bench §4 b) shows which side is right. | D1 |

**Order of the PRs:** D2a, D2b and D2h can go first, in parallel. D2e comes next. D2c and D2d follow D2b. D2f follows D2e, and D2g comes last.

## Repair rules (D2c)

Each rule needs its evidence before it fires, and logs what it changed.

| Rule | Evidence that must be present | Change | PR |
|---|---|---|---|
| `direction_marks` | U+200E LRM, U+200F RLM or U+061C ALM in office XML text | removed (logged) | D2c1 |
| `latin1_eth_nun` | ð next to Hebrew letters, in a font with no נ anywhere in the document (cp1255 0xF0 read as Latin-1). Decided **per font**, not per file, because one file can mix a broken font and a healthy one. | ð → נ | D2c1 |
| `rtl_bracket_order` | In the document's RTL text-layer lines, mirroring every bracket makes the bracket order valid in at least 3 lines, and in more than 3 times as many as reading them as read. Decided per document. A paragraph balanced as read and broken when mirrored is left as read. On the dev split, 1.6% of RTL bracket paragraphs were valid as read. | every bracket in RTL lines mirrored | D2c1 |
| `font_private_use` | **Dropped in D2c2 for lack of evidence.** On the dev split, no font with broken private-use codes names its glyphs. The 2016 David subset (F01A = ם and so on) has no `post` and no `cmap` table; the other David (F8FF for נ) has `post` format 3, which carries no names. Private-use codes from symbol fonts (Wingdings, Symbol, Webdings, Dingbats) are signs, not letters, and no longer count as broken. The remaining T3 pages go to D2d. | — | D2c2 |
| `symbol_font_bullet` | a private-use symbol-font code at line or cell start, before a space | → • (moved from D1) | D2b |

**Digit groups are flagged only.** A token such as `000,005,8` is flagged `digit_groups_suspect`, and the text is not changed. It has two readings: reversing every character gives 8,500,000, and reversing the group order gives 8,005,000. The volunteer saw both. The repair comes in D3, with the number cross-check, and only where an independent reading confirms one of the two.

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
