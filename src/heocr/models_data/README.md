The GitHub install has no bundled OCR weights. These notices document the models
from earlier distributions, pinned in `heocr/models.py`:

- `heocr_modern_v1.traineddata`: modern and office print. Notice: `NOTICE.heocr_modern_v1`.
- `heocr_oldprint_v1.traineddata`: old and rabbinic print. Notices: `NOTICE.heocr_oldprint_v1`, and
  `NOTICE.heocr_oldprint_v1.r7`, since it continues R7.

When the requested bundled model is unavailable, heocr falls back to stock
Tesseract and records `bundled_model_unavailable`.

Rivok Full and Lite v2.0 int8 are optional downloads from Rivok Labs. See their
[component model card](MODEL_CARD.heb_rivok.md),
[licence](LICENSE.heb_rivok) and [notice](NOTICE.heb_rivok).


The four measured models used by `--ocr-model auto` are explicitly installed
with `heocr --download-model auto`. Their immutable revisions, SHA-256 pins, DOIs
and Apache-2.0 licence are in `heocr/models.py`. The Hugging Face releases
include each model's licence and notices.
