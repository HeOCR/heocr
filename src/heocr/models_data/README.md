Models shipped inside the package (D6, decided 2026-11-18). Each is a released HeOCR model pinned in
`heocr/models.py` (`bundled=True`), Apache-2.0, with its notice beside it:

- `heocr_modern_v1.traineddata`: modern and office print, the route table's `modern` domain. Notice: `NOTICE.heocr_modern_v1`.
- `heocr_oldprint_v1.traineddata`: old and rabbinic print, the `old` domain. Notices: `NOTICE.heocr_oldprint_v1`, and
  `NOTICE.heocr_oldprint_v1.r7`, since it continues R7.

The model files are not committed. `scripts/fetch_models.py` fetches them, checked against their pins, before a wheel
is built. The notices are committed as they are published with the models.
