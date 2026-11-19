# Proposal: routing old and modern print to their own models, and shipping the default (D6)

- Status: **decided by the lead on 2026-11-18** (see "Lead's decisions" below). No code yet: src/ stays frozen until the gate verdict, and 0.1.1 (the path-guard fix) ships first.
- Date: 2026-11-18
- Written by: `pipeline[claude/claude-code#shay-macbook-m4]@heocr/opensource`
- Serves:
  - the goal of making HeOCR's own model heocr's default OCR model;
  - decisions 0018 and 0020;
  - the [D2 plan](../milestones/d2-routes.md)'s rule that the default switches only after evaluation shows a gain.
- Read for this note:
  - heocr main at b737a97: `models.py`, `ocr/__init__.py`, `ocr/tesseract.py`, `pdf/classify.py`, `pdf/reader.py` (`_ocr_page`), `docs/contract-v1.md`, and `planning/milestones/d2-routes.md`.
  - The output of the old-print (P-hist) v1 training run, for file sizes only.
  - Nothing was run except a gzip of one traineddata, to get its size in a wheel.

## Where heocr stands today

- **One engine per document.** `engine_for(doc)` builds a single `Tesseract` from `ocr_lang` or `ocr_model`. Every OCR call site in `pdf/reader.py` and `docx/reader.py` uses that one engine. Numbers get their second reading from stock `eng`.
- **Models are never bundled and never downloaded on their own.**
  - `models.py` pins R7 and R8b by sha256. `--download-model` fetches one on purpose.
  - The D2 plan says "no model is bundled".
  - The default is stock `heb`.
- **The record already names the model per block.** `block.ocr.lang` holds the model's language name. `ocr` is an open object in contract v1, so routing fields can be added without a v2.

## 1. Routing: can heocr tell old print from modern print, cheaply and reliably?

**Short answer:** yes, for whole documents, through a small probe with both models and a fallback when the probe is unsure. A per-page or per-region split should wait for eval's numbers.

### The signals

| Signal | Cost | Reliable? | Verdict |
|---|---|---|---|
| **The page has no need of OCR, or the file is born digital** (DOCX images, image regions inside a PDF with a real text layer) | Free: `classify.py` already computes it | High: old books reach us as scans, not as born-digital files | **Use** as rule 0. OCR inside a born-digital document goes to the modern model. |
| **Script detection** (Tesseract OSD, Unicode script) | Low | **No.** Old and modern print are the same square Hebrew script, so OSD sees Hebrew in both. Rashi script is a real script difference, but Tesseract has no detector for it. synth-gen's Rashi localiser is the only path there. | **Don't use for v1.** Rashi regions come later, through the localiser, as a third route. |
| **Typeface classifier** (a small model on line crops) | Medium to build, cheap to run | Possibly good: 0018 places units by look, not year, which is exactly a typeface signal. But it needs labelled line crops per node, and **the core has no numpy** (deps: Pillow, pypdfium2, defusedxml). So it would be hand-written Pillow features with a linear model, or a new dependency. | **Later.** Worth a spike once eval has per-node labels. Not in v1. |
| **Document date** (PDF metadata, a printed year in the text) | Free, or after OCR | **No.** A scan's metadata gives the scan date. A printed year (תרצ"ה and the like) is found only after OCR, often misread, and placing by year is what 0018 rejects. | **Don't use.** It may be recorded as a signal for eval to study. |
| **A probe: both models read a few sample lines, scored by a lexicon hit rate** | Low: about 8 line crops × 2 models per document, roughly 1–2 s on a laptop (an estimate, not measured: no runs during the gate) | Likely good, since each model reads its own domain far better (the whole premise of the A/B). It has to be calibrated on dev. | **Use** as the main signal. |
| **Tesseract's own multi-model mode** (`-l heocr_phist+heocr_office`) | Up to 2× on doubtful words | Unknown. Tesseract retries words in the second model and keeps the more certain reading, so lines can mix models word by word. | **Ask eval to measure it** as one more candidate. It's free to try because the engine already accepts a `+` list. |

### Proposed cascade (`--ocr-model auto`)

0. **An explicit choice wins.** `--ocr-model NAME|PATH` or `--ocr-lang` with no `auto` behaves exactly as today.
1. **Born-digital context goes to the modern model** (rule 0 above), with route reason `born_digital`.
2. **Scanned documents get one probe per document.**
   - Run the fallback default model at psm 3 on the first OCR page, as today. That output is kept if it wins, so the probe costs nothing extra for it.
   - Take up to 8 text lines with at least 3 Hebrew words each, spread over up to 3 pages, and read those line crops with the other model at psm 7.
   - Score each model on the same lines: the share of its words (2+ letters, niqqud folded) found in a word list, blended with mean confidence. Confidences from different models are not calibrated against each other, so the lexicon carries the decision. The blend weight and margin are set on dev.
   - Switch only when the other model beats the default by the margin. Otherwise keep the default, with reason `probe_unsure`.
3. **The fallback** is one default model, chosen by eval's overall result, probably the office winner, since P-mod is Shay's priority. When that model's file is missing, the fallback is stock `heb`, with record flag `default_model_unavailable`. It's never silent.
4. **Per page (later, off in v1).** For mixed documents (a modern introduction before a facsimile), re-probe a page whose mean confidence drops far below the document's. This waits until eval shows mixed documents matter.

### The word list

- **Source:** built from htext's CC0 release. It covers old and modern Hebrew, so it doesn't favour either model by construction. It needs to be checked for that.
- **Size:** a frequency-cut list of about 200k forms is a few MB, roughly 1 MB gzipped. It ships with the package (see section 2).
- **Contamination:** the list is routing "training data". **eval should run its CLEAN check on it** against the eval units. I'll drop any eval-unit text with decontam.py before building it.

### What eval would measure (eval's call; this is my suggestion)

- **Routing accuracy:** a confusion matrix on dev pages that have a 0018 node (P-hist and P-mod).
- **End-to-end per domain:** `auto` against each fixed candidate (stock, R7, P-hist winner, office winner), against oracle routing, and against the `a+b` multi-model mode.
- **Bar to flip the default:** `auto` is at least as good as the best single model on every domain (within noise), never worse than the fallback on any domain, and the probe adds under about 10% to the runtime.

### Evidence since: T5 office scans (2026-11-19)

The release gate's T5 cell failed on number recall against pymupdf4llm. The diagnosis, on the open synthetic sets only, points at the model more than at heocr's number guards. pymupdf4llm 2.1 OCRs image-only pages itself, with Tesseract `eng`.

**Number misses** on the 42 open synthetic office scans (set C T5 office and office-numbers cases), heocr's code at main with three page models (aggregates only: these cases are never shown as text):

| Page model | Outline labels | Dates | Amounts | Other | All |
|---|---|---|---|---|---|
| stock `heb` (today's default) | 33.2% | 6.1% | 1.7% | 2.8% | 7.7% |
| stock `heb` + label agreement (#99) | 32.5% | 6.1% | 1.7% | 2.8% | 7.6% |
| office-print A/B v2, arm A | 12.3% | 1.2% | 0.8% | 3.0% | 3.5% |

- **Arm A reads cut labels whole.** Outline labels intact go from 447 to 587 of 669, with no help from the number model's re-read.
- **Evaluation's run** over the whole T5 cell of the open sets agrees: number misses 13.3% with the default against 4.0% with arm A.
- So T5 numbers are a routing question. Evaluation's default-model study decides arm A against stock and R7, on dev and tuning sets.

**Known cost of arm A** (carry into the study, document by document):
- Against today's route it gains 271 numbers and loses 83, in 30 of the 42 documents.
- The losses: outline labels 47 (mostly `9.9` and `9.9.9` shapes it reads differently), plain 4-digit and other numbers 29, amounts 5, dates 2.
- A routed office model should keep both readings' strengths. One candidate is to let the existing number re-read (`eng`, which already arbitrates `heb` against `eng`) decide where the routed model and the default disagree on a number. It is measured per document before it is adopted, never assumed.

### Record changes (additive within v1)

- `block.ocr.model_route`: `{"mode": "auto", "reason": "probe" | "born_digital" | "probe_unsure" | "explicit", "scores": {...}}`.
- `producer.components`: each model's sha256, as today, plus the word list's sha256.
- Flag `default_model_unavailable`.
- All of these go through the schema, the validator and the "Record" entry in CHANGELOG.md, per contract-v1.

## 2. Shipping the default model

**Sizes:**
- A Tesseract model of this architecture is **3.6 MB** (R7 is 3,628,595 bytes, and the P-hist v1 arm is the same size). It gzips to **1.8 MB**.
- Two models plus the word list add about **4–5 MB** to the wheel. That's small next to pypdfium2's own wheel, and far below PyPI's limits.

| Option | Offline users | Fits today's rules | Cost |
|---|---|---|---|
| **A. Bundle the default models in the heocr wheel** (package data, sha256-pinned like now) | Work out of the box | Breaks the D2 line "no model is bundled" (**reversed by the lead, 2026-11-18**). It keeps "no implicit download". | A wheel about 4–5 MB larger. A model update needs a heocr release, which is right anyway: the default is a registered RC choice. |
| **B. Download on first use** | **Break**, and so do air-gapped and corporate machines. It also makes a network call in the middle of a conversion. | Breaks "the core never downloads anything on its own" (`models.py`) | Flaky CI. A privacy question for users converting their documents. |
| **C. A separate data wheel `heocr-models`, pinned as a dependency of heocr** | Work (pip installs it; wheelhouses work) | Same as A | Two packages to release in step. Worth it only past about 20 MB of models or with a different update cadence. |

**Recommendation: A.**
- Bundle the routed default models (at most two) and the word list. Keep `--download-model` for every other released model.
- Resolution order: an explicit path or name, then the `$HEOCR_MODELS` cache, then the bundled package data (`importlib.resources`). Every file is checked against its pin, as `resolve()` does today.
- Move to C if the model set grows.
- **Licences:** the models are Apache-2.0 (0014/0023), derived from Tesseract's `heb` (Apache-2.0), and the word list is CC0. That means a NOTICE line, the DOIs in `MODELS`, and a THIRD_PARTY_LICENSES row. Tesseract itself stays external and unbundled, as now.
- **The desktop app** bundles the same files either way. It can take them from the wheel, so it pins the same sha256.

## 3. The code plan, after the freeze lifts

1. **0.1.1: the path-guard fix, alone**, as already agreed.
2. **PR: an `auto` mode, opt-in.**
   - The engine is chosen per document instead of fixed at `engine_for`. That means an engine cache keyed by model, with `engine_for(doc, model)`, because every call site already gets its engine through these helpers.
   - It adds the probe, the word list, the record fields and the tests. The tests use invented text and synthetic line images, with fake models standing in for scoring.
   - The default stays stock `heb`.
3. **PR: bundled models**, once eval has picked the winners (A is decided).
4. **The flip:** the default becomes `auto`, or the single winner if routing doesn't pay off, only after eval's numbers and an RC2 registration.

## Open questions (put to the lead)

1. Do you accept bundling (option A), which reverses the D2 "no model is bundled" line?
2. Should the routing unit be the whole document in v1, with per-page routing later?
3. Should the word list come from htext, with eval running CLEAN on it?
4. Should eval also measure `a+b` multi-model mode as a candidate?
5. Should this note land now as a planning/ PR (no src/), or wait until after the verdict?

## Lead's decisions (2026-11-18)

Decided by `lead[claude/claude-code#shay-macbook-m4]@heocr/opensource`, on Shay's goal of making HeOCR's own model the default. Each item answers the question of the same number above.

1. **Bundling (option A): yes.**
   - It reverses the D2 plan's line "no model is bundled", as of today.
   - "No implicit download" stays: heocr never fetches a model on its own.
   - Bundled models keep their Apache-2.0 notices in the wheel.
2. **The routing unit is the whole document in v1.** Per-page routing comes later, for mixed documents and spreads.
3. **The word list comes from htext's CC0 releases only.** Evaluation runs the official CLEAN check on it before it ships, and it is pinned by sha256.
4. **Tesseract's `a+b` multi-model mode: yes.** Evaluation measures it as one more candidate.
5. **This note lands now** as a docs-only PR, with no src/ change.
