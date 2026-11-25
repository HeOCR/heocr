# heocr: a user guide

How to install heocr, read documents with it from Python or the command line, and read what it tells you. The full record is described in [record.md](record.md), and the formats and routes in the [README](../README.md).

## Install

heocr needs Python 3.11 or later and Git. Install the development version from GitHub:

```sh
pip install git+https://github.com/HeOCR/heocr
```

HeOCR has no current public package or model release. We will publish a release only after heocr beats existing readers on at least one document type.

The GitHub install has no bundled OCR models. With Tesseract and Hebrew language data installed, the default OCR falls back to stock `heb`. The CLI warns and the record includes `bundled_model_unavailable`. The warning is printed even with `-q`. Use `--ocr-model heb` or `ocr_model="heb"` to select stock explicitly.

The core needs only pypdfium2, Pillow and defusedxml. It reads text-layer PDFs, DOCX, XLSX, PPTX and HTML on its own. Two optional programs add the rest:
- **Tesseract,** for scans, images and image regions;
- **LibreOffice,** for DOC, RTF, ODT, ODS, XLS, ODP and PPT.

Without them, heocr still runs: the parts that need them are reported in `unread` (`ocr_route_unavailable`, `converter_unavailable`), never guessed.

### Tesseract

heocr needs Tesseract's `heb` model, and `eng` for the second reading of numbers.

**macOS (Homebrew):**

```sh
brew install tesseract tesseract-lang
```

**Debian and Ubuntu:**

```sh
sudo apt install tesseract-ocr tesseract-ocr-heb tesseract-ocr-eng
```

**Windows:** install Tesseract from the [UB Mannheim builds](https://github.com/UB-Mannheim/tesseract/wiki), and tick Hebrew under the additional languages. If `tesseract.exe` is not on the `PATH`, point heocr at it with the environment variable `HEOCR_TESSERACT`, or with `tesseract=` / `--tesseract`.

**Which `heb` model:** system packages often ship `tessdata_fast`'s `heb`. To use `tessdata_best`'s instead, put it in a folder of your own with `eng`, and point `TESSDATA_PREFIX` at that folder:

```sh
mkdir -p ~/tessdata
curl -L -o ~/tessdata/heb.traineddata https://github.com/tesseract-ocr/tessdata_best/raw/main/heb.traineddata
curl -L -o ~/tessdata/eng.traineddata https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata
export TESSDATA_PREFIX=~/tessdata
```

To check, run `tesseract --list-langs`: it should list `eng` and `heb`. heocr records the models it used, each with its sha256, in the record's `producer.components`.

### LibreOffice

**macOS:**

```sh
brew install --cask libreoffice
```

heocr also finds it in `/Applications`.

**Debian and Ubuntu:**

```sh
sudo apt install libreoffice-writer libreoffice-calc libreoffice-impress
```

**Windows:** install LibreOffice from [libreoffice.org](https://www.libreoffice.org/download/). If `soffice.exe` is not on the `PATH`, set `HEOCR_SOFFICE` to its path, or pass `soffice=` / `--soffice`.

Each conversion runs with a throwaway profile and stops after 180 seconds (`convert_timeout` / `--convert-timeout`).

## Which flags for which document

Start with `heocr INPUT`. HeOCR reads usable text layers and uses local OCR
where it needs to read pixels. Add a route when the input calls for one.

| Document or goal | Flag or setup | What to expect |
| --- | --- | --- |
| Readable PDF, office file or routine scan | No route flags | The default chooses the text layer, format reader or installed local OCR. |
| Angled standalone phone photo of printed Hebrew | `--profile phone-specialist` | Adds layout and guarded perspective correction. Declare `--ocr-capture-type phone` if automatic detection misses the photo. Requires local modern_v1. [Phone specialist profile](#phone-specialist-profile) |
| Known scanner or issuer-render capture | `--ocr-router v2 --ocr-capture-type scanner` or `--ocr-router v2 --ocr-capture-type issuer-render` | Uses pinned Rivok Full where OCR is needed. [Capture router v2](#capture-router-v2) |
| Scan with columns, mixed-direction lines or possible tables | `--ocr-layout v1 -f json` | Applies geometric layout to OCR. JSON keeps the structure and flags. [Geometric OCR layout](#geometric-ocr-layout) |
| Borderless table in a PDF with a usable text layer | Install `heocr[tables]`, then pass `tables="tableformer"` and `tables_models=path` to `heocr.extract` | Python API with local model weights. [Learned tables extra](#learned-tables-extra) |
| Scanned book in historical Hebrew | `--ocr-router v2 --ocr-old-print-model PATH` | Tries the pinned local P-hist v2 on OCR pages with corroborating historical evidence. [Historical print](#historical-print-with-router-v2) |
| Experimental hosted OCR for an approved document | Start with `--ocr-hybrid haiku --ocr-hybrid-dry-run` | Local cost estimate. The hybrid route is parked for launch. [Haiku hybrid OCR](#haiku-hybrid-ocr) |

### Capture router v2

Use `heocr scan.pdf --ocr-router v2` or `heocr.extract("scan.pdf", ocr_router="v2")` to enable capture routing. PDF pages and regions that need OCR use the pinned Rivok Full v2.0 int8 model. JPEGs with camera exposure or focal-length EXIF use `heocr_modern_v1`. When that metadata is absent, router v2 also checks for a strongly foreshortened paper outline in RGB JPEG or PNG images. Inputs without a capture signal use the usual model route.

You can override classification with `--ocr-capture-type` or `ocr_capture_type=`. For example, use `heocr scan.png --ocr-router v2 --ocr-capture-type scanner` for a known scanner image. `phone`, `iphone` and `pixel` pick modern_v1, and `scanner` and `issuer-render` pick Rivok. The capture declaration applies to the whole input and also works on its own. Keep the model, language and alternate-backend options at their defaults. The OCR record includes the capture signal and chosen model. When the classifier or declaration picks a pinned model, a missing or mismatched local file is reported as unavailable.

The PDF reader still uses readable text layers. Use `--force-ocr` to read a rendered PDF page in place of its text layer. Declare phone captures wrapped in PDF to select modern_v1.

Install the optional Rivok Labs model with `heocr --download-model heb_rivok`. HeOCR checks the download before keeping it in the model cache (`HEOCR_MODELS` when set). OCR reads the local file. The router stays opt-in while carrier-matched DEV validation is pending.

See the [HeOCR component model card](../src/heocr/models_data/MODEL_CARD.heb_rivok.md) for the immutable weight pin and upstream credits. The package includes Rivok's [Apache-2.0 licence](../src/heocr/models_data/LICENSE.heb_rivok) and [NOTICE](../src/heocr/models_data/NOTICE.heb_rivok).

Add `--ocr-line-v6 /path/to/v6.traineddata` or pass `ocr_line_v6=` in Python to enable the two-model reread. With router v2, it applies to prose from Rivok page OCR, so you can pass the same option for a batch with mixed input captures. You can also pair it with `--ocr-capture-type scanner`, `--ocr-capture-type issuer-render`, or `--ocr-model heb_rivok`. The option rereads existing prose lines and preserves their geometry and order. It expects the pinned local v6 checkpoint with SHA-256 `52b94ef13eaa3c8d32403ab05e12fc9be74d22a2d1aaa4d2de904b313b0e1609`. HeOCR records the hash of the staged model and the selections on changed lines. It retains the page reading when v6 is unavailable or a reread is inconclusive. Rereading adds two recognition calls per eligible prose line.

Add `--ocr-line-batch` or pass `ocr_line_batch=True` to send the same Rivok
and v6 crops in groups of up to 64 per page. This uses the same chooser and
document record as individual reads. HeOCR retries a group individually if
Tesseract fails or its TSV cannot be matched to the input crops. An explicit
document time limit keeps individual reads so it can be checked between crops.

Add `--ocr-number-batch` or pass `ocr_number_batch=True` to batch the
number model's single-word rereads during full-page OCR. Each Tesseract
process loads the number model for up to 64 crops. HeOCR keeps the existing
number checks and uses individual reads when a group fails or a document
time limit applies.

#### CPU cost

Timing used the same ten DEV raster pages on each system, five phone photos and five issuer renders. Router v2 used declared captures and the pinned v6 reread on the five Rivok pages. The default reader used modern_v1. Standalone Rivok ran Tesseract once per raster with the same Rivok int8 weights and page segmentation mode 3. HeOCR's number rereads were disabled for the timing.

| System | Reader | Pages/min | Peak RSS (MiB) |
| --- | --- | ---: | ---: |
| Apple M4 Max, macOS 15.7.4, Tesseract 5.5.3 | HeOCR default | 37.44 | 753.88 |
| | HeOCR router v2 with v6 | 14.68 | 742.39 |
| | Standalone Rivok int8 | 42.77 | 356.45 |
| GitHub Ubuntu 24.04 runner, Xeon Platinum 8370C, Tesseract 5.3.4 | HeOCR default | 16.43 | 494.43 |
| | HeOCR router v2 with v6 | 8.11 | 493.57 |
| | Standalone Rivok int8 | 28.13 | 262.45 |

Pages/min is the median of three serial batches with Tesseract limited to one thread. Peak RSS is the maximum summed resident memory of the reader and its child processes, sampled every 20 ms. The timing includes program startup and varies with page size and the number of prose lines available for rereading.

#### Photo grayscale and Otsu

Use `--photo-preprocess gray-otsu` (Python `photo_preprocess="gray-otsu"`) to send grayscale pixels to the main Tesseract reader and explicitly select its built-in Otsu thresholding. It applies to standalone images recognized by the capture classifier or declared as `phone`, `iphone` or `pixel`. For example, use `heocr photo.jpg --ocr-router v2 --ocr-capture-type phone --photo-preprocess gray-otsu`. The OCR options record the setting on eligible input. Page geometry, DPI, model selection and scan routes use their usual settings.

#### Phone perspective correction

```sh
heocr photo.jpg --ocr-router v2 --ocr-capture-type phone --ocr-phone-geometry
```

Use this experimental option for angled phone photos of printed Hebrew prose.
The reader uses supported paper edges and native OCR word positions to find
the text plane. It rereads individual straightened lines at a common sampling
scale and maps their positions back to the source image. The route keeps the
native reading when the paper or line geometry is uncertain. Declared scanner
and issuer-render inputs bypass this option. On enabled phone pages, individual
number crops can share an OCR batch when no document time limit is set. The
pinned modern v1 recognizer with Tesseract 5.5.3 omits rectified crops whose
readings would be discarded by the geometry checks. In Python, pass
`ocr_phone_geometry=True`. A page with an accepted reread records
`ocr_phone_geometry_rectified` in its flags.

For internal capture checks, see the [DEV215 triad runbook](dev215-triads.md).

### Phone specialist profile

```sh
heocr photo.jpg --profile phone-specialist
heocr photo.jpg --profile phone-specialist --ocr-capture-type pixel \
  --phone-model /local/models/specialist.traineddata --phone-model-sha256 SHA256
```

Use this local profile for standalone phone images. It combines capture
routing, layout v1 and guarded perspective correction. Router v2 recognizes
camera JPEGs by exposure or focal-length EXIF and can detect a strong paper
perspective in RGB JPEGs and PNGs. Declare `phone`, `iphone` or `pixel` for a
standalone image without either signal. Other inputs follow the
same route and yield the same record with or without the profile. For a phone
photo wrapped in a PDF,
[declare the capture with router v2](#capture-router-v2) to select modern_v1
for OCR. The profile applies its geometry only to standalone images.

The profile needs the pinned modern_v1 model locally. Put
`heocr_modern_v1.traineddata` in the model cache or point `HEOCR_MODELS` at
its directory. The native page pass uses modern v1 (PSM 3). Supported rectified
crops use modern v1 by default (PSM 7 at 300 DPI). Supply both the local
traineddata path and its full SHA-256 to swap only the crop recognizer. HeOCR
checks the file on routed phone inputs and rechecks the staged copy before
recognition.
A bad pin or a failed custom recognition batch is an error. When geometry or
individual line evidence is inconclusive, the profile keeps the native
reading. The profile makes no downloads or hosted OCR calls.

In Python, pass `profile="phone-specialist"` and optionally
`phone_model=` and `phone_model_sha256=`. The phone record binds the profile
and its configured line SHA-256. Accepted rectified reads also record
`ocr_phone_geometry_line_sha256` in the page signals.

#### Fast phone specialist profile

```sh
heocr photo.jpg --profile phone-specialist-fast
```

Use the experimental fast profile when you can trade some rereading for speed.
It retains local phone geometry, layout and targeted number checks. It skips
the edge-padding page retry and full-page English sweep for number and Latin
corrections. When using the pinned modern_v1 weights with Tesseract 5.5.3,
it also keeps a native line as read if the line maps to one rectified band and
every word has confidence of at least 95. The profile accepts the same capture
declaration and local line-model options as the phone specialist.

### Geometric OCR layout

```sh
heocr scan.pdf --ocr-layout v1 -f json
```

Choose this stage for layout on OCR pages. Pair it with
[`--ocr-router v2`](#capture-router-v2) if you also want capture-based model
selection. The phone specialist profile enables layout v1 automatically.

The optional layout stage uses OCR boxes to read sustained prose columns
right to left and attach recurring narrow margin notes at adjacent rows.
It preserves spanning headings and footers when their placement is clear.
Ruled tables use isolated cell reads. Stable unruled tables with an amount
or ID column can also produce explicit cells in top-to-bottom rows, with
cells ordered from the right in Hebrew.

For mixed Hebrew lines, the stage uses the word boxes to keep a bounded
Latin or numeric expression in logical left-to-right order between Hebrew
anchors. It leaves the characters within each recognized word intact.
When visible ink reaches an image or cell-crop edge, it may retry with a
paper-colored border. The retry must preserve the existing text and numeric
runs and add glyphs at an edge. Its boxes map back to the source coordinates.
The stage can help recognize strokes that remain visible at the boundary.

The Python option is `ocr_layout="v1"`. It applies to OCR, and readable
text layers use their usual route. The record flags accepted ordering and
padding changes.

### Learned tables extra

Use this experimental Python option for tables without visible rulings
on PDF pages with a usable text layer. TableFormer proposes table and
cell boxes. HeOCR fills the cells from the page's own text and gives
native ruled grids first claim. For tables in scanned pages, use
[geometric OCR layout](#geometric-ocr-layout).

Install the `tables` extra, which adds Docling and PyTorch. With the
Hugging Face CLI (`hf`), fetch the two model bundles at the pinned revisions.

```sh
pip install 'heocr[tables] @ git+https://github.com/HeOCR/heocr.git'
hf download docling-project/docling-models --revision fc0f2d45e2218ea24bce5045f58a389aed16dc23 \
  --local-dir table-models/docling-project--docling-models
hf download docling-project/docling-layout-heron --revision 8f39ad3c0b4c58e9c2d2c84a38465abf757272d8 \
  --local-dir table-models/docling-project--docling-layout-heron
```

Pass the parent directory of the two bundles to the Python API.

```python
import heocr

doc = heocr.extract("tables.pdf", tables="tableformer", tables_models="table-models")
print(doc.markdown)
```

HeOCR checks the weight hashes and uses the local models. Enable TableFormer
through these Python arguments. Installing the extra alone leaves the default
table reader in use. See the [model pins](../src/heocr/tableformer.py).

### Historical print with router v2

```sh
heocr book.pdf --ocr-router v2 --ocr-old-print-model /private/phist_v2.traineddata
```

Set up the [local router v2 models](#capture-router-v2) first. The optional
branch probes whole-page OCR for historical Hebrew print by looking for
distinct classical-print terms on multiple lines of the local
reading. It requires the P-hist v2 reread to corroborate the terms, text
coverage and confidence. Digit-heavy pages, ruled tables, declared native
issuer renders and pages with weak evidence keep the usual router v2
reading. Readable PDF text layers keep their existing route.

Provide the local Apache-licensed P-hist v2 file with SHA-256
`240716ac4c253d264c1fa62d81c207e1313fda59a99ae79c1e21f7f0509feded`.
The package contains no copy of these weights. It verifies the staged
file and keeps the router v2 reading when the file or reread is unavailable.
Selected pages record the stable `phist_v2` model label and digest. In Python
pass `ocr_router="v2"` and `ocr_old_print_model=path`.

### Haiku hybrid OCR

**Experimental, parked for launch.** The line- and page-level routing trials
missed the joint target of near-Haiku-only quality at half the API cost.
Use the local routes for routine reading.

The option remains available. `--ocr-hybrid haiku` explicitly authorizes
HeOCR to send OCR images to Anthropic using `ANTHROPIC_API_KEY`. Use it only
for documents you are authorized to share. For an estimate without sending
anything, add `--ocr-hybrid-dry-run`.

```sh
heocr scan.pdf --ocr-hybrid haiku --ocr-hybrid-max-usd 1.00 --ocr-hybrid-dry-run
heocr scan.pdf --ocr-hybrid haiku --ocr-hybrid-max-usd 1.00
heocr old-print.png --ocr-hybrid haiku --ocr-hybrid-old-print
```

The route pins `claude-haiku-5-5`, Anthropic's fixed Haiku 5.5 snapshot ID.
Anthropic publishes this snapshot ID without a date suffix. HeOCR checks
that the response reports the same ID. [Anthropic migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide)

The fixed prompts live in the package. HeOCR sends an oriented RGB PNG of each
image frame or rendered PDF page that needs full-page OCR. It strips source
metadata, limits the long edge to 2,200 pixels and writes a fixed 200 DPI tag.
Readable text layers and embedded regions use their
usual routes. Use `--force-ocr` to send rendered pages with readable PDF text
layers as well. The old-print switch also sends a local HeOCR R8b draft using
PSM 3 and 200 DPI. Install the pinned seed once with
`heocr --download-model heocr_r8b`. Hosted output is unverified page text and
has no word boxes or table structure. Use `--no-repairs` to read its raw text.

The cap defaults to USD 1 per input document. Before every request, HeOCR
reserves USD 0.544768 at the highest uncached rates for a 1,048,576-token input
and 8,192 output tokens. It replaces that reserve with reported list-price
usage when available. An unknown charge keeps its reserve. The dry run
prints a conservative JSON estimate using the document's possible page count
and the cap. It reads only local metadata and works without a key. A cap below
one reservation permits zero hosted calls. [Anthropic model pricing and limits](https://platform.claude.com/docs/en/models/haiku-5-5/overview)

HeOCR uses its configured local OCR when the key or R8b seed is unavailable,
the budget has insufficient room, or the API cannot return a complete
transcript. A transport or protocol error stops hosted calls for the rest of
that document. The JSON record includes the model ID, prompt hash, budget
usage, and per-page hosted or fallback status. In Python, use
`heocr.extract("scan.pdf", ocr_hybrid="haiku", ocr_hybrid_max_usd="1.00")`.
Add `ocr_hybrid_old_print=True` for old print.

#### Confidence-gated line correction

With router v2, add `--ocr-hybrid-confidence SCORE` to read locally first and
send only selected prose line images and their OCR drafts to Anthropic. This
option requires a score from 0 to 100. The score is the arithmetic mean of
the local OCR word confidences for that reading. It is not a probability.
Lines below the threshold are eligible. A score of 0 sends nothing.

```sh
heocr scan.pdf --ocr-router v2 --ocr-layout v1 --ocr-hybrid haiku --ocr-hybrid-confidence 70
```

The example score is illustrative. Supply the same options with
`--ocr-hybrid-dry-run` for a budget upper bound. The reader sends up to 32
of the lowest-scoring line crops in one request per OCR page, preserving
local reading order, boxes and digit sequences. Table cells, lines lacking
a compatible local confidence, and readable text layers stay local. The
document cap and fallback apply to the whole batch. JSON records include
eligible, below-threshold, submitted, accepted and changed line counts.
In Python, pass `ocr_router="v2"`, `ocr_hybrid="haiku"` and
`ocr_hybrid_confidence=70`. This route is separate from Haiku old-print
dispatch and the local historical-model route.

#### Example API cost per input

The November 24, 2026 DEV log recorded one request per selected image.
Repricing its uncached token counts at the logged USD 0.10 per million input
tokens and USD 0.50 per million output tokens gives these costs.

| Route and input | Inputs | Mean USD per input | Observed USD range |
| --- | ---: | ---: | ---: |
| Haiku image only, modern page photos | 10 | 0.00094 | 0.00065–0.00149 |
| Haiku image plus R8b draft, historical line panels | 8 | 0.00184 | 0.00154–0.00234 |
| The same mix of photos and panels | 18 | 0.00134 | 0.00065–0.00234 |

The mixed example is $1.34 per 1,000 input images at the same proportions.
Historical panels contain selected lines. Full books and different page sizes
can have different costs. Local routes use $0 in API charges and still use CPU
time. Use the dry run for the conservative reservation and check each
successful page's `signals.ocr_hybrid_usage.list_price_usd` in the JSON record for its
reported token cost.

## From Python

The examples here use two small invented files, `notice.pdf` and `letter.docx`.

```python
import heocr

doc = heocr.extract("notice.pdf")       # a path or the file's bytes
print(doc.text)
```

```text
הודעה על פתיחת הליך
המועד להגשת הצעות הוא 15.03.2027
הסכום המרבי הוא 250,000 ש"ח
```

- `doc.text`: the plain text in reading order. Pages are separated by a form feed (`\f`). Headers, footers, notes and navigation follow the body, in their own sections.
- `doc.markdown`: headings, numbered sections and tables kept, with a comment marking each page.
- `doc.to_json()` / `doc.to_dict()`: the `heocr_document.v1` record.
- `doc.to_docx()` (bytes) / `heocr.write_docx(doc, path)`: a Word file. Paragraphs are right to left, with Hebrew runs marked right to left, in Arial (which has Hebrew letters in Word, LibreOffice and Google Docs). Headings use Word's Heading 1–6 styles. List items keep their labels as read, written as text. Tables are Word tables, right to left when the table is. Each new page starts on a new page. Notes, comments, headers and footers follow the body in their own sections, as in `doc.text`, under Hebrew titles when most of the body is Hebrew. Footnote references are not marked in the body (as in `doc.text`). Figures that were not read leave no trace, as in `doc.text`.
- `doc.blocks`, `doc.pages`, `doc.unread`, `doc.flags`: the same record as Python objects.

`heocr.validate(record)` checks a record (a dict) against the contract and returns a list of errors, which is empty when it is valid.

Use up to eight workers for batches of independent image files. The iterator yields a
complete document record for each input, in input order, using the same options
as `extract`. Process each record as it arrives to keep memory bounded.

```python
for doc in heocr.extract_images(["page-01.png", "page-02.png"], workers=2):
    print(doc.text)
```

Each worker runs Tesseract on one thread. Time limits still apply per input, and
running jobs concurrently can change when an explicit deadline is reached.

The options, all keyword-only:

| Option | Default | |
|---|---|---|
| `ocr` | `True` | Run OCR where the text exists only as an image. `False` reports those pages unread. |
| `ocr_lang`, `ocr_psm` | `"heb"`, `3` | Tesseract's languages and page segmentation mode |
| `tesseract` | found | The Tesseract program, when it is not on the `PATH` |
| `ocr_model` | `"heocr_modern_v1"` | The GitHub install has no bundled models and falls back to stock Tesseract with `bundled_model_unavailable`. Choose `"heb"`, `"stock"` or `None` to select stock explicitly, or pass a local `.traineddata` path. |
| `force_ocr` | `False` | Read every PDF page by OCR, its text layer ignored (`--force-ocr`); flags each page `ocr_forced`. Needs `ocr` |
| `check_numbers` | `"suspect"` | The second reading of numbers (below) |
| `repairs` | `True` | `False` turns every repair rule off: the text exactly as extracted |
| `convert`, `soffice`, `convert_timeout` | `True`, found, 180 s | LibreOffice for the older office formats |
| `max_pages` | 2,000 | Pages or image frames read at most (below) |
| `time_limit` | none | Seconds for the whole document (below) |

## From the command line

```sh
heocr notice.pdf                     # text to standard output
heocr letter.docx -o letter.md       # the format follows the suffix: .md, .json, .docx, else text
heocr scan.pdf -o scan.docx          # a Word file (or -f docx > scan.docx)
heocr notice.pdf -f json             # the record
heocr scan.pdf --ocr-lang heb+eng    # Tesseract's languages
heocr notice.pdf --no-ocr            # never run OCR; pages that need it are reported unread
heocr book.pdf --force-ocr           # read every page by OCR, ignoring its text layer
heocr --help                         # every option
```

The CLI prints each `unread` entry on standard error, and `-q` silences them. It exits with 1 only when the file cannot be opened or an option is invalid. A document read only in part, or not at all, exits with 0, and its `unread` entries say why.

## Experimental CPU line recognition

From a checkout, install the optional runtime with `pip install '.[crnn]'`. Supply a compatible
local ONNX model and its SHA-256 digest. The optional runtime and its dependencies have their own licence terms.

```sh
heocr scan.pdf -f json --ocr-model heb --crnn-model reader.onnx --crnn-sha256 "$READER_SHA256"
```

In Python, use `heocr.extract(path, crnn_model=model, crnn_sha256=digest)`. Tesseract provides
page segmentation, line boxes and reading order. The CRNN reads those printed Hebrew prose lines
from the working raster. Tesseract reads table cells, separately recovered form entries and
embedded image regions. Text-layer pages follow the usual routing rules. CRNN blocks record the
engine, the model checksum and unverified status. Missing readings from the model are flagged.

Models use an ONNX float32 input of shape `[1, 1, 32, width]` and logits of shape
`[1, width/4, classes]`, with dynamic width and time. Each grayscale crop is resized
proportionally to 32 pixels high, with width rounded up to a multiple of four and capped at
2,048. Input pixels use `1 - pixel/255`. Set ONNX metadata `heocr.crnn.schema=heocr.crnn.line.v1`
and `heocr.crnn.alphabet` to a JSON array of unique single-character strings in visual-order
CTC class order after the blank class at index zero. HeOCR decodes to a logical Hebrew line.

## Experimental handwriting OCR

Install the optional Kraken 7.2 runtime with `pip install '.[handwriting]'` from the repository. Use local, authorized model weights and their SHA-256 pins. HeOCR reads them offline. The optional runtime and its dependencies have their own licence terms.

For a scan already known to be handwritten, use the bundled, checksum-verified Kraken segmenter with a local recognizer:

```sh
heocr manuscript.pdf -f json --handwriting recognizer.safetensors \
  --handwriting-sha256 "$RECOGNIZER_SHA256" --handwriting-mode force
```

Automatic mode defaults to type tags. Pass `--handwriting-segmenter MODEL.safetensors` and `--handwriting-segmenter-sha256 HEX`. Every segmented text line must carry the `handwriting` or `handwritten` type tag for the page to enter the Kraken route. Mixed and untyped pages take ordinary OCR with this detector.

To opt into the experimental geometry detector, pass `--handwriting-detector geometry` with the recognizer and its SHA-256. It checks whole-page stroke widths, baseline regularity, and component shapes before loading Kraken. Pages that fail the gate take ordinary OCR. This detector and force mode can use the bundled segmenter. The geometry gate applies to image-only pages and image frames. Its behavior on mixed handwriting and print is unverified.

In Python, pass `handwriting=heocr.HandwritingConfig(model, sha256, mode="force")` to `heocr.extract`. To use the tags detector, provide `segmenter` and `segmenter_sha256` to the automatic config. To use the geometry detector, specify `detector="geometry"`. All forms require OCR to be enabled.

Successful lines are emitted in Kraken's reading order as `source: ocr` and `ocr.engine: kraken`, with page-point boxes and unverified confidence. Kraken and both model digests appear in the record. Wide images receive white padding below the original pixels to bound the segmenter's internal width. Boxes are clipped to the original image. The `handwriting_*` flags record the route or its fallback. Backend failure, an empty or incomplete reading, and padding beyond the pixel budget fall back to ordinary OCR. Ink outside recognized lines can mark a successful reading partial.

## Reading the record

A block of `notice.pdf`, from `heocr notice.pdf -f json`:

```json
{"id": "b0", "kind": "text", "role": "body", "source": "text_layer", "tool": "heocr.pdf", "flags": [], "repairs": []}
```

Its page: `{"index": 0, "class": "T1", "route": "text_layer"}`.

**Where the text came from.**
- `source` is the route: `text_layer`, `office_xml`, `converted_docx` / `converted_xlsx` / `converted_pptx`, `html` or `ocr`.
- `tool` is what read it: `heocr.pdf`, or for OCR, the Tesseract version.
- For a PDF, each page's `class` (T1–T6) and `route` say how it was read.
- An OCR block also has `ocr`: its engine, settings, confidence and `status: "unverified"`. OCR text is never presented as verified.

**Roles.**
- `body` is the document's text.
- `header` and `footer` are running headers, footers and page numbers.
- `footnote`, `endnote` and `comment` are notes, tied to their place by `note_refs`.
- `textbox` holds shapes outside the flow, `navigation` web menus, and `notes` speaker notes.
- Only `body` and `textbox` make up the main flow of `doc.text`. The others follow it, so a reader that only wants the body can filter by role.

**Repairs** fix characters the source meant, but its font or producer broke, such as reversed brackets or `ð` for `נ`. Each one fires only on evidence. It is logged in the block's `repairs`, with the rule, the code points and the counts. `repairs=False` turns them all off.

**Flags** mark where the reading is a guess, or where heocr kept something as read because the evidence was too thin. For example:
- a one-line document with one bracketed phrase can't show whether its producer mirrored brackets, so the block is flagged `rtl_brackets_unverified` and its brackets are left as the layer holds them;
- `unruled_table`: a table inferred from layout alone;
- `ocr_short_for_ink`: ink that OCR left unread;
- `reading_order_unresolved_columns`: side-by-side text in an unknown order.

The full list is in [record.md](record.md).

**`unread`** is what was not read. Each entry has a `scope` (`document`, `page` or `block`), a `reason`, and where possible the `page` and a `bbox`. A document is only fully read when `unread` is empty. Check it before trusting the text as complete.

## Limits: `max_pages` and `time_limit`

`max_pages` (2,000 by default; `--max-pages`) bounds the pages or image frames read from one document, since each may cost an OCR run. The rest become one `unread` entry, and the document is flagged `page_limit_reached`:

```sh
heocr chapters.pdf --no-ocr --max-pages 2
```

```text
heocr: unread: page_limit (pages 3-3 of 3 not read (max_pages=2))
```

`time_limit` (none by default; `--time-limit SECONDS`) is a safety net for the whole document, conversion and OCR included. It is off by default, so the same file always gives the same record, whatever the machine's speed. When set, it is checked before each page, image frame, embedded image and page of number checks. The step under way finishes, since each Tesseract and LibreOffice call has its own timeout. What is left is never dropped:
- pages or frames not read become one `unread` entry with the reason `time_limit`;
- an embedded image not read becomes a `figure` block with its own `time_limit` entry;
- numbers left unchecked flag `numbers_not_checked`.

The document is flagged `time_limit_reached`, and the limit is recorded in `options.time_limit`.

```python
doc = heocr.extract("long-scan.pdf", time_limit=600)
if "time_limit_reached" in doc.flags:
    print([u.detail for u in doc.unread if u.reason == "time_limit"])
```

## The number checks

Numbers matter most in the documents heocr is built for, so it reads them a second time. Every second reading is logged next to the first, and a reviewer or a program can revert it.

**On text-layer PDFs,** `check_numbers` chooses which lines are read again by OCR of their own image:

| `check_numbers` | |
|---|---|
| `"suspect"` (default) | Only lines with a reversed-looking digit group (`000,005,8`): the evidence the `reversed_digit_groups` repair needs |
| `"tables"` | Also every table cell with an amount, a percentage or a date |
| `"all"` | Every line and cell with them; trustworthy disagreements are flagged |
| `False` (`--check-numbers off`) | No second reading |

A digit group is repaired only when the second reading confirms one of its two readings (`digit_groups_confirmed`). Otherwise it stays as read, flagged `digit_groups_suspect`. A block whose numbers disagree with the second reading is flagged `number_disagreement`, and one whose numbers could not all be confirmed is flagged `numbers_unconfirmed`.

**On OCR pages,** words with digits are read again by Tesseract's `eng` model, the number model, which reads digits better than `heb`. So are number segments, cut section labels, and the whole page once more. A reading replaces heb's only on fixed rules:
- the number model must be confident, or the label sequence must vouch for it;
- it never adds text that heb did not read.

Each replacement is in the block's `checks.number_readings`, with both readings, their confidences and the model, and the block is flagged `numbers_from_second_reading`. A reading that was not used is logged with `used: false`.

In scanned tables, each number cell keeps every model's reading in `readings`. A cell whose readings differ, neither confident, is flagged `table_cell_unconfirmed`. Cells that read as no number in a column of numbers, or as noise, are flagged too, and listed in `unread`.

Without the `eng` model, heb reads the numbers alone, and the document is flagged `numbers_read_with_main_model`.

## When something goes wrong

- **A page you expected text from is empty:** look at `unread` and at the page's `class` and `route`. `ocr_route_unavailable` means Tesseract or its `heb` model was not found.
- **An office file is not read:** `converter_unavailable` means LibreOffice was not found. `conversion_timeout` means it ran out of time.
- **Brackets, numbers or labels look wrong:** look at the block's `repairs`, `flags` and `checks`, and try `repairs=False` to see the text exactly as extracted.

To report a problem, see [the release notes](release-notes-0.1.0.md#reporting-issues). Never attach a real document or paste its text.
