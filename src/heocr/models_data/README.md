The GitHub install has no bundled OCR weights. These notices document the models
from earlier distributions, pinned in `heocr/models.py`:

- `heocr_modern_v1.traineddata`: modern and office print, the route table's `modern` domain. Notice: `NOTICE.heocr_modern_v1`.
- `heocr_oldprint_v1.traineddata`: old and rabbinic print, the `old` domain. Notices: `NOTICE.heocr_oldprint_v1`, and
  `NOTICE.heocr_oldprint_v1.r7`, since it continues R7.

When the requested bundled model is unavailable, heocr falls back to stock
Tesseract and records `bundled_model_unavailable`.
