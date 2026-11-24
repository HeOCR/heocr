# Thursday DEV215 capture and scoring runbook

The ten-page office packet has three slots per physical sheet. Keep the input exports, normalized rasters, references, receipts and scores in private DEV custody outside the checkout.

## Pull the selected originals

1. Get the reviewed print manifest and its SHA-256 from the capture owner. Use the printed ID to find each leaf and confirm that all three captures show the same physical sheet.
2. Export the individual DEV chat from Telegram Desktop. Select the originals shared as files. For macOS Photos, use **Export Unmodified Original**. For Google Photos, download the selected items. Place scanner originals in a separate export folder. The script pulls only the selected paths from these local exports. [Telegram Desktop export](https://telegram.org/blog/export-and-more) · [Apple Photos export](https://support.apple.com/en-us/guide/photos/pht6e157c5f/mac) · [Google Photos downloads](https://support.google.com/photos/answer/7652919?hl=en)
3. Save the selection in a private file with the shape below. Map the phone slots from the checklist or device confirmation. The example maps phone A to iPhone. Use the actual mapping for the batch. Set `print_instance` to the same pseudonymous ID for the three captures of one sheet. Set `page_index` to the zero-based page of a scan PDF, or 0 for an image. Set `rotation` to the clockwise correction in degrees.

```json
{
  "schema": "heocr.office.selection.dev215.v1",
  "split": "dev",
  "print_manifest_sha256": "<reviewed print manifest SHA-256>",
  "devices": {
    "scanner": {"id": "office-1", "type": "scanner"},
    "phone-a": {"id": "phone-a", "type": "iphone"},
    "phone-b": {"id": "phone-b", "type": "pixel"}
  },
  "captures": [
    {
      "print_id": "DEV215-01",
      "print_instance": "sheet-01",
      "slot": "phone-a",
      "provider": "telegram",
      "file": "files/selected-original.jpg",
      "page_index": 0,
      "rotation": 0,
      "original": true
    }
  ]
}
```

```sh
python scripts/dev215_triads.py register \
  --print-manifest "$print_manifest" --print-sha "$print_sha" \
  --selections "$selection" --selection-sha "$(shasum -a 256 "$selection" | cut -d ' ' -f 1)" \
  --telegram "$telegram_export" --photos "$photos_export" --scanner "$scanner_export" \
  --out "$private_intake"
```

Pass just the export roots used by the selection. Paths under each root are relative. The registration lists missing slots and binds complete raw bytes, page index, rotation, packet hash, source ID and device type. Raw originals keep their bytes and metadata. Each receipt is immutable and named `registration-<SHA-256>.json`. The same inputs reproduce the same receipt. As captures arrive, register a cumulative selection and pass its final receipt to eval.

## Freeze the working rasters

Use the reviewed DEV preparation procedure. Bind the selected raw page and rotation to its working PDF and 144-DPI opaque RGB PNG. White-mask the witness regions, or record that the approved crop excludes them. Review the page-to-carrier geometry, reference line and cell joins, and logical order. Record the lineage and reference-review receipt hashes. Each sheet shares one ordered reference and one common mask across its three captures.

Save the scorer inputs under one private asset root. The freeze is a JSON object with these fields.

| Level | Required fields |
| --- | --- |
| Top level | `schema` = `heocr.office.score-freeze.dev215.v1`, `split` = `dev`, `registration_sha256`, `source_freeze_sha256`, `lineage_receipt_sha256`, `review_receipt_sha256`, `heocr_commit` (full Git hash), `tesseract_sha256`, `eng_sha256`, `heb_sha256`, `scorer_sha256`, `dependencies`, `pages` |
| `dependencies` | Exact installed versions keyed by `Pillow`, `pypdfium2`, `defusedxml`, and `rapidfuzz` |
| Each of the ten `pages` | `print_id`, `source_id`, `issue_cluster`, `reference` (relative JSON path), `reference_sha256`, `captures` (object keyed by the three capture slots) |
| Each capture asset | `file` (relative PNG path), `sha256`, `size` (width and height in pixels), `raw_sha256`, `page_index`, `rotation`, `geometry_receipt_sha256`, `witness_boxes` (top-left pixel boxes as `[left, top, right, bottom]`), `witness_outside_crop` (boolean) |
| Each reference JSON | `schema` = `heocr.office.reference.dev215.v1`, `split` = `dev`, `print_id`, `source_id`, `lines` (strings in reviewed page order), `keep` (one boolean per line for the frozen common mask) |

Eval supplies the freeze SHA-256 after review. For a cropped-out witness, set `witness_outside_crop` to true and `witness_boxes` to an empty list. For a retained frame, use false and supply the white-mask boxes. Resolve partial triads and unclear joins before freezing.

## Score the phone branch

Use the clean HeOCR checkout, Tesseract runtime and three weight files named in the freeze and runner. Pass eval's registered `page_cer.py` next to its pinned `line_fold_v1.json`. Run the command first with `--preflight-only`, then run it without that flag.

```sh
python scripts/dev215_triads.py score \
  --registration "$receipt" --registration-sha "$registration_sha" \
  --freeze "$freeze" --freeze-sha "$freeze_sha" --assets "$private_assets" \
  --checkout "$heocr_checkout" --models "$model_dir" --line-v6 "$v6_traineddata" \
  --tesseract "$tesseract" --tessdata "$stock_tessdata" --scorer "$page_cer" \
  --out "$private_scores" --preflight-only
```

The scorer runs router v2, fixed modern_v1 and fixed Rivok through the same HeOCR path. It declares iPhone and Pixel routes explicitly, so the masked PNGs can omit EXIF. Router v2 uses modern_v1 for both phones and Rivok int8 with v6 on eligible scanner prose. It emits counts and route receipts. The primary comparison is common-mask micro CER with paired intervals resampled by source issue. WER, number misses and all-reference CER are descriptive. Carry the result to the decision table below.

## Go/no-go table

| Gate | Evidence to fill in | Status |
| --- | --- | --- |
| Capture roster | Print manifest, source freeze and registration SHAs. Complete sheets ___/10. Scanner ___/10, iPhone ___/10, Pixel ___/10. | GO / HOLD |
| Reviewed scoring scope | Eval freeze SHA. Lineage, geometry, witness and common-mask receipts verified. | GO / HOLD |
| Reader and model pins | HeOCR commit, scorer SHA, Tesseract and weights verified. Scanner pages with v6 ___/10. | GO / HOLD |
| iPhone branch | Text parity with fixed modern_v1 ___/10. Note any discrepancy. | GO / HOLD |
| Pixel branch | Text parity with fixed modern_v1 ___/10. Note any discrepancy. | GO / HOLD |
| Quality review | Complete the carrier table and record the reviewer decision and rationale. | GO / HOLD / NO-GO |

| Thursday DEV stratum | Router v2 CER | Fixed modern CER | Fixed Rivok CER | Paired deltas and issue intervals | WER / missed numbers | Decision notes |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Scanner | | | | | | |
| iPhone | | | | | | |
| Pixel | | | | | | |
| Pooled triads | | | | | | |

A failed prerequisite or branch parity check holds the handoff. Use the quality review to decide whether the phone branch is ready on the Thursday subset. The wider DEV215 coverage and any v1 scoring decision have their own approval.
