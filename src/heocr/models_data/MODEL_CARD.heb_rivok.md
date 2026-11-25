# Rivok Full v2.0 int8 in HeOCR

Rivok Labs released this Hebrew, English and digit recognition model for
Tesseract. It descends from Google's `tessdata_best` `script/Hebrew` model.
HeOCR uses the upstream weights unchanged. The upstream [LICENSE](LICENSE.heb_rivok)
and [NOTICE](NOTICE.heb_rivok) preserve the Apache-2.0 terms and credits for
the model and its source lineage.

| Field | Value |
| --- | --- |
| HeOCR model name | `heb_rivok` |
| Upstream revision | `9253cacf097ab396a2fc36d633737c43af5c28a3` |
| Weight file | [`tessdata/heb_rivok.traineddata`](https://huggingface.co/Rivok/tesseract-heb-rivok/resolve/9253cacf097ab396a2fc36d633737c43af5c28a3/tessdata/heb_rivok.traineddata) |
| Size in bytes | 5,678,607 |
| SHA-256 | `09ef1830f3393c590791abf9c18deca14450c8e5d3b556249d63b0183cf7a426` |
| Documentation | [Upstream model card at this revision](https://huggingface.co/Rivok/tesseract-heb-rivok/blob/9253cacf097ab396a2fc36d633737c43af5c28a3/README.md) |

Install with `heocr --download-model heb_rivok`. Use `--ocr-router v2` to
select it for PDF regions that need OCR, or declare its capture type with
`--ocr-capture-type scanner` or `--ocr-capture-type issuer-render`.
The model is optional and omitted from
HeOCR's wheels. The router remains opt-in pending carrier-matched DEV validation.

Use a distinct filename for a custom or adapted checkpoint. The name
`heb_rivok.traineddata` identifies the exact int8 release above in HeOCR.

## Rivok Lite v2.0 int8

The experimental pointed category can use the Hebrew-only Lite model from
the same upstream revision and licence. Lite derives from the upstream
Tesseract `heb` model and retains its own codec. The optional download
`heocr --download-model heb_rivok_lite` verifies a size of 3,486,882 bytes
and SHA-256 `59da073ad0bca9449fa5a965996e49d0e2bee3c17e8b0b4e2769d67a7ce34c40`.
[Pinned Lite weights](https://huggingface.co/Rivok/tesseract-heb-rivok/resolve/9253cacf097ab396a2fc36d633737c43af5c28a3/tessdata/heb_rivok_lite.traineddata)
