# Local release regression checks

Run `heocr regress` before each release candidate. Use the same frozen bundle for the control and the candidate. The bundle contains synthetic engineering fixtures produced from the hdocsyngen pilot. Keep its files, reports, and counts private. The release benchmark runs separately.

## Run a frozen bundle

Use the release candidate's environment with Tesseract and its stock Hebrew and English models installed. Use the same executable and models for both arms.

```sh
heocr regress "$BUNDLE/manifest.json" --manifest-sha256 "$BUNDLE_SHA256" \
  --report "$PRIVATE_REPORT" --baseline "$KNOWN_REPORT" --jobs 4
```

The baseline is optional. A first run without it records the existing findings for review. Archive that report with the control commit, tool versions, and bundle pin before using it as an accepted baseline. The command verifies the manifest and all asset hashes, checks that the baseline used the same bundle and Tesseract toolchain, and creates a local JSON report. Exit `0` means no new findings, `1` means findings need review, and `2` means the run could not be validated. Use `heocr -- regress` to extract a document whose filename is literally `regress`.

The harness reads P10 first, then mixed-direction amounts, P9 grain, P8, and scanned labels. It checks the P10 layer decision, body reading order, logical text and mixed-direction runs, bracket orientation, numbers and amounts, and the table grid. It uses the producer's observed numbering labels. Cases without observed labels can still check text, order, and table cells. The report lists unscored features where producers lacked usable gold or left text partially rendered.

Review a finding against the pinned PDF and producer gold. If it exposes a code defect, use a minimal fixture built from invented text for the issue or fix PR. Keep the corpus files, excerpts, and QA measurements in the private run record.

## Freeze a new bundle

Use the verified hdocsyngen pilot and its stored derivative records. The producer checks and label basis are documented in the [hdocsyngen verifier guide](https://github.com/HeOCR/hdocsyngen/blob/main/docs/producers.md). The P8 receipts cover the stored Ghostscript outputs, including outlined text and a PostScript round trip. Use those exact bytes.

```sh
python scripts/prepare_regress.py "$PILOT" "$BUNDLE" \
  --pilot-sha256 "$PILOT_SHA256" \
  --p8-records "$P8_RECORDS" --p8-sha256 "$P8_SHA256"
```

The script selects verified producer outputs and their gold in chain order. It checks derived page geometry and writes relative asset paths, SHA-256 hashes, and producer lineage to the manifest. Store the bundle and manifest pin together in the private release QA location. Freeze a new pin when the selection changes.

This check is a release-candidate step. Per-PR CI runs invented unit fixtures for the harness itself.
