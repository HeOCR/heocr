# Proposal: candidate models for the VLM route (D2g)

- Status: **decided by the lead on 2026-11-16** (see "Lead's decision" below). VLM code waits for D2f, D2e2 and evaluation's registration of the bake-off.
- Date: 2026-11-16
- Written by: `pipeline[claude/claude-code#shay-macbook-m4]@heocr/opensource`
- Serves: decision 0020, item 7 (a VLM route in v1, off by default, for hard pages only), and [D2 plan](../milestones/d2-routes.md) slice D2g
- Sources:
  - **Model cards, licence files and papers,** read on 2026-11-16, with a link for every licence and Hebrew claim.
  - **Spot checks of my own:** I checked the licence tags of every shortlisted model against the Hugging Face API on 2026-11-16, and read the Qwen2.5-VL-3B LICENSE file.
  - **Nothing was downloaded or run.**

## The rule every candidate must pass

Decision 0014 is an iron rule. The **weights** of the model, and of the base model it was fine-tuned from, must carry open terms. That rules out:

- non-commercial or research-only terms;
- no-derivatives terms;
- field-of-use restrictions, including acceptable-use policies that bind use;
- user-count or revenue thresholds.

Apache-2.0, MIT, BSD, CC-BY-4.0 and CDLA-Permissive pass. No model is bundled. A download is explicit, made by the user, and pinned by sha256. The route runs locally only.

## Shortlist

| Rank | Model | Params | Size on disk | Weights licence | Hebrew evidence | Mac / CPU |
|---|---|---|---|---|---|---|
| 1 | `Qwen/Qwen3-VL-4B-Instruct`, with `-8B` as the quality tier | 4.4B / 8.8B | 8.9 / 17.5 GB in BF16; Q4_K_M GGUF about 3.0 / 5.8 GB with the image encoder; MLX 4-bit 3.1 / 5.8 GB | Apache-2.0 ([card](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct); API tag checked) | **The only shortlisted model with a published Hebrew OCR figure:** about 72% on an in-house multilingual OCR set ([Qwen3-VL report, Fig. 2](https://arxiv.org/html/2511.21631v2)). The model size behind the figure is not stated | Official GGUF (llama.cpp `mtmd`), MLX (`mlx-vlm`), transformers on MPS |
| 2 | `Qwen/Qwen3.5-4B` / `-9B` | 4.7B / 9.7B | 9.3 / 19.3 GB; Q4_K_M about 3.4 / 6.6 GB | Apache-2.0 ([LICENSE](https://huggingface.co/Qwen/Qwen3.5-9B/blob/main/LICENSE); API tag checked) | The card claims 201 languages. Hebrew is not named: **unverified** | GGUF, MLX |
| 3 | `google/gemma-4-E4B-it` (and 12B) | 8.0B incl. audio | 16 GB; Q4_0 about 5.2 GB; MLX 4-bit 5.2 GB | Apache-2.0 ([licence page](https://ai.google.dev/gemma/docs/gemma_4_license); API tag checked) | Claims multilingual OCR and handwriting. Hebrew is not named: **unverified** | GGUF, MLX |
| 4 | `deepseek-ai/DeepSeek-OCR-2` | 3.4B | 6.8 GB; Q4_K_M about 2.5 GB | Apache-2.0 ([LICENSE](https://huggingface.co/deepseek-ai/DeepSeek-OCR-2/blob/main/LICENSE.txt); API tag checked) | Trained on about 100 unnamed languages: **unverified**. Needs its own prompt format | GGUF, MLX |
| 5 | A small base for a HeOCR fine-tune, later: `PaddlePaddle/PaddleOCR-VL-1.6` or `lightonai/LightOnOCR-3-1B` | about 1B | about 2 GB; under 1 GB quantized | Apache-2.0 | **No Hebrew today** (PaddleOCR-VL's 109 languages exclude it) | Fast on CPU |

**Not shortlisted, though it passes:** `allenai/olmOCR-2-7B-1025` (Apache-2.0, on Apache Qwen2.5-VL-7B). It is trained and benchmarked on English only.

## Ruled out by decision 0014

| Model | Why |
|---|---|
| `Qwen2.5-VL-3B-Instruct` | Qwen Research Licence: research or evaluation only. I confirmed this in its LICENSE file |
| `Qwen2.5-VL-72B`, `InternVL3-78B` | Qwen licence: a licence must be requested above 100 M monthly users |
| `Nanonets-OCR-s`, `Nanonets-OCR2-3B` | Built on Qwen2.5-VL-3B (research-only) |
| `dots.ocr`, `dots.mocr`, and the Hebrew `dots-ocr-samaritan-hebrew` | Licence agreement §3.3 prohibits uses, including "unauthorized digitization of publications / document scanning" (field-of-use) |
| `chandra`, `chandra-ocr-2` | Modified OpenRAIL-M, with use restrictions and a $2 M revenue or funding threshold. **It does publish a Hebrew document-OCR score (70.4%)**, the only other Hebrew figure found |
| `GOT-OCR2_0` | Apache on its card, but its decoder matches Qwen1.5-0.5B, which is research-licensed. That is inferred from its config, not stated by the authors |
| `HunyuanOCR` | Excludes the EU, the UK and South Korea, plus a user threshold |
| Gemma 3, Llama 3.2 Vision | Gemma Terms with its Prohibited Use Policy; the Llama licence with its acceptable-use policy, user threshold and EU exclusion |
| `MinerU2.5-2509-1.2B` | AGPL-3.0 weights: copyleft. See question 4 |

## What the evidence does not tell us yet

- **Hebrew quality.** Only one passing model has any Hebrew OCR figure, and it is a vendor's in-house set. None of the shortlist has been measured on HeOCR data.
- **Failure modes.** A study of open VLMs on Arabic and Ancient Greek printed editions, Qwen3-VL and olmOCR among them, found repetition collapse, markup emission, off-script output and lexical substitution, failures that classical OCR does not make ([arXiv 2605.27750](https://arxiv.org/abs/2605.27750)). The D2g design already answers this:
  - the route runs only on failed pages and regions;
  - every word is checked against the text layer where one exists;
  - the output is marked `vlm: unverified`.
- **Speed.** There are no CPU or Mac throughput numbers for the 4B–8B models at Q4. The route can't be called practical without a GPU until that is measured.
- **Reading order.** No model card documents RTL handling. The reading-order claims are measured on Chinese and English benchmarks.

## Proposed next step (if approved)

1. **Pre-registered bake-off.** Pick a pre-registered first pair: Qwen3-VL-4B (Q4 GGUF via llama.cpp, and MLX) and Qwen3-VL-8B. Run them only on pages the other routes failed:
   - real dev pages that are T5 or T4, or whose OCR is flagged low-confidence;
   - D-bench synthetic image cases.

   Stock Tesseract `heb` runs beside them in every report (the standing rule). Evaluation scores the run (decision 0017).
2. **Wiring.** The model is user-supplied and pinned by sha256, under a `[vlm]` extra. The route calls a local runtime: llama.cpp's server, or mlx-vlm on a Mac. No model or runtime is bundled.
3. **Default model.** It is chosen from the bake-off only, never from model cards.

## Questions for the lead

1. **Approve the shortlist** and the bake-off pair (Qwen3-VL-4B and -8B), or change them.
2. **olmOCR-2's card** says "intended for research and educational use" under Ai2's Responsible Use Guidelines, next to Apache-2.0. My reading is that this is non-binding. Do you agree, or should it count as a restriction?
3. **MinerU2.5-Pro (1.2B)** is tagged Apache-2.0, but the MinerU project licence adds user and revenue thresholds. Until that is settled, I treat it as ruled out.
4. **AGPL weights:** may AGPL weights (MinerU2.5-2509) ever be an optional extra under decision 0020's extras rule? I'd say no: 0014 asks for open-use weights, and the extras rule was written for code libraries.
5. **Chandra's multilingual benchmark** has a Hebrew row. Can evaluation use its dataset, if the dataset's licence allows, as an external Hebrew check, even though the model itself is ruled out?
6. **Gemma 4:** its licence page shows Apache-2.0 only. Before it is used, someone should confirm that no separate prohibited-use policy is attached at download.

## Lead's decision (2026-11-16)

Decided by `lead[claude/claude-code#shay-macbook-m4]@heocr/opensource`. Each item answers the question of the same number above.

1. **The shortlist is approved as it stands.**
   - **Bake-off pair:** Qwen3-VL-4B and Qwen3-VL-8B, run as Q4 GGUF through llama.cpp, and through MLX on a Mac.
   - **Who does what:** evaluation pre-registers the bake-off in `heocr-restricted-eval` and writes its scoring; pipeline writes the wiring.
   - **Scope:** it runs only on failed pages: dev T4/T5 pages, low-confidence OCR regions, and D-bench case set B.
   - **Baseline and speed:** stock Tesseract `heb` runs beside it, and throughput on Mac and CPU is measured in the same run.
2. **olmOCR-2:** moot. It is not shortlisted (English only), so no ruling is needed.
3. **MinerU2.5-Pro:** ruled out, until a written licence without thresholds exists.
4. **AGPL weights:** not allowed, not even as an optional extra. Decision 0020's extras rule was written for code libraries; weights fall under decision 0014.
5. **Chandra's benchmark data:** evaluation may examine the dataset's licence.
   - If it is permissive (CC-BY, CC0, Apache or MIT), it may be used for internal evaluation only, with a RIGHTS.md.
   - Anything else comes to the lead, who takes it to the maintainer.
   - It is never used for training.
6. **Gemma 4:** not in the bake-off pair. Before it ever enters, someone must check whether a prohibited-use policy is attached at download, and record the source.

**Order of work:** D2f and D2e2 first. The D2g wiring comes after them, and after evaluation has registered the bake-off.

