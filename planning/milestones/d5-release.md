# D5 — Release readiness: the first PyPI release, after the D-bench gate

- Status: planned
- Owner: `pipeline[claude/claude-code#shay-macbook-m4]@heocr/opensource`; the lead reviews and merges (heocr-engine decision 0001)
- Serves: heocr-engine decisions 0019 and 0020; proposal `document-pipeline-package.md` §7 (release)
- Builds on: D1–D4
- GitHub: milestone `D5 - Release`, label `roadmap:D5`

## The gate

**The release gate is eval's D-bench report.** It covers heocr at `ed96cc7` or later, against the baselines, on:
- CER/WER in reading order;
- table cells;
- exact numbers;
- silent loss.

**Pre-registration.** Eval pre-registers the release gate (proposal §5.4) before using it: the measures, the baselines and the thresholds are fixed first, and only then is heocr scored. D5f waits on that pre-registered gate and on the report against it.

Nothing is published before the report and before the lead's and Shay's go-ahead. Every other slice below can be done before the gate. None of them is the gate.

## Where D5 starts (2026-11-17, dev split, counts only)

- **Timing** (M4 Max, Tesseract 5.5.3), with the load recorded during the run. Seconds per page, median / p95:
  - text-layer PDF pages: 0.024 / 0.058 without OCR, 0.026 / 0.065 with it;
  - text-layer pages with region OCR: 0.156 / 0.368;
  - whole-page OCR: 0.81 / 0.83.
  - Per document: DOCX 0.02 s without OCR and 0.15 s with it; DOC through LibreOffice 2.8 s.
- **Packaging dry run:**
  - the sdist and the pure-Python wheel build;
  - a fresh install, from either, pulls only defusedxml, pypdfium2 and Pillow;
  - the CLI reads the synthetic DOCX, PDF, HTML, XLSX, PPTX and PNG fixtures;
  - NOTICE ships in the wheel.
  - There are no pip extras: Tesseract and LibreOffice are separate programs.

## Slices

Each slice is its own PR from main.

| Slice | Content |
|---|---|
| **D5a** | **Packaging in CI.** A job builds the sdist and the wheel, installs the wheel into a fresh venv on Linux, macOS and Windows, runs the CLI on the synthetic fixtures, and runs `twine check`. Metadata: classifiers for Python 3.11–3.13, project URLs, and a version scheme starting at `0.1.0`.<br>**A licence audit**, recorded in the repository: each runtime dependency and every binary it bundles (pypdfium2 ships PDFium; Pillow ships its image libraries), with its licence, so the wheel's licence story is checked rather than assumed. |
| **D5b** | **The contract, frozen.** `heocr_document.v1` becomes a JSON Schema file in the repository. A test checks that every record from the test suite validates against it, as well as against `validate()`. A short rule says what a v1 change may be (adding optional fields only), and when v2 is needed. |
| **D5c** | **Before the release:**<br>• **rotated pages:** the displayed frame (#52);<br>• **a per-document time limit**, as a safety net: an option and a flag, with the rest of the document in `unread`;<br>• **T6's cost:** if it shows in the timing, a cheap check before the full cell OCR. Today it costs 0.16 s per page median on dev. |
| **D5d** | **Hostile input.** One test module gathers the probes from the D4 reviews: deep nesting in HTML and PPTX, huge spans and merges, ZIP and size limits, page limits, a converter that hangs, and text outside the page. Each one must end with a bounded time and a record.<br>**No corpus text in the repository.** A check that no file or test fixture holds text from the private corpus: fixtures stay synthetic, and their text is invented.<br>**The ported-code rule, enforced in CI:** `git grep -i -E "tendex|govil|amster|yonatan"` is empty, and "adanim" appears only in NOTICE's general line (Shay's decision, 2026-11-18). |
| **D5e** | **Docs.** The README is done (#50). Still to come: a CHANGELOG from D1 to D4; a short reference for the record (fields, roles, sources, flags, unread reasons) next to the schema; and CONTRIBUTING (licences, synthetic fixtures only, tuning on dev only). |
| **D5f** | **The release**, after eval's pre-registered gate and its report: a tag, GitHub release notes, TestPyPI first, then PyPI. **The publishing accounts (TestPyPI, PyPI) and the publish itself are Shay's go-ahead.** The accounts come through the Codex helper. |

## Exit criteria

- Eval's D-bench report passes the gate the lead sets with eval.
- The wheel installs and runs the CLI smoke test on Linux, macOS and Windows in CI.
- The schema file, the CHANGELOG and the record reference are in.
- No known way, among the D4 probes, to make heocr hang, crash or lose text silently.

## Not in D5

- **D2g (the VLM route)** waits for eval's registration of the bake-off.
- **EPUB, email and archives** are identified but not read. Each would need its own plan.
