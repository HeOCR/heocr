"""``heocr.extract``: one call, any document."""

from __future__ import annotations

import hashlib
import math
import os
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from functools import partial
from itertools import islice
from pathlib import Path

from heocr import contract
from heocr.contract import MAX_PAGES, Document, Unread
from heocr.convert import CONVERSIONS, CONVERT_TIMEOUT
from heocr.handwriting import HandwritingConfig, HandwritingReader
from heocr.identify import identify
from heocr.ocr.tesseract import DEFAULT_LANG, DEFAULT_PSM, _temporary_models, check_settings

# The default OCR model: HeOCR's modern-print model, bundled in the package (``heocr.models``).
DEFAULT_MODEL = "heocr_modern_v1"
# Short names for the bundled models.
MODEL_ALIASES = {"modern": "heocr_modern_v1", "oldprint": "heocr_oldprint_v1"}
# ``ocr_model`` values that read with stock Tesseract ``ocr_lang`` (the escape hatch from the default).
STOCK_MODELS = ("heb", "stock")

IMAGE_FORMATS = ("png", "jpeg", "tiff", "gif", "bmp", "jp2", "webp")
# Inputs above this size are refused before parsing. Real tender documents are far smaller.
MAX_FILE_BYTES = 512 * 1024**2

Reader = Callable[[bytes, Document], None]
Source = str | os.PathLike[str] | bytes


def _source_bytes(source: Source) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        body = bytes(source)
    else:
        path = Path(source)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"input is {size} bytes; the limit is {MAX_FILE_BYTES}")
        body = path.read_bytes()
    if len(body) > MAX_FILE_BYTES:
        raise ValueError(f"input is {len(body)} bytes; the limit is {MAX_FILE_BYTES}")
    return body


def _readers() -> dict[str, Reader]:
    """Formats with a route, mapped to the reader that fills a Document in place."""
    from heocr.convert import CONVERSIONS, read_converted
    from heocr.docx.reader import read_docx
    from heocr.htmlread import read_html
    from heocr.images import read_image
    from heocr.pdf.reader import read_pdf
    from heocr.pptx import read_pptx
    from heocr.xlsx import read_xlsx

    return {"docx": read_docx, "pdf": read_pdf, "html": read_html, "xlsx": read_xlsx, "pptx": read_pptx, **{fmt: read_converted for fmt in CONVERSIONS}, **{fmt: read_image for fmt in IMAGE_FORMATS}}


def extract(
    source: Source,
    *,
    repairs: bool = True,
    ocr: bool = True,
    ocr_lang: str = DEFAULT_LANG,
    ocr_psm: int = DEFAULT_PSM,
    tesseract: str | Sequence[str] | None = None,
    ocr_model: str | None = DEFAULT_MODEL,
    ocr_router: str | None = None,
    ocr_layout: str | None = None,
    ocr_old_print_model: str | os.PathLike[str] | None = None,
    ocr_hybrid: str | None = None,
    ocr_hybrid_old_print: bool = False,
    ocr_hybrid_confidence: float | None = None,
    ocr_hybrid_max_usd: str | Decimal | None = None,
    ocr_capture_type: str | None = None,
    ocr_line_v6: str | os.PathLike[str] | None = None,
    ocr_line_batch: bool = False,
    ocr_number_batch: bool = False,
    photo_preprocess: str | None = None,
    ocr_phone_geometry: bool = False,
    profile: str | None = None,
    phone_model: str | os.PathLike[str] | None = None,
    phone_model_sha256: str | None = None,
    force_ocr: bool = False,
    crnn_model: str | os.PathLike[str] | None = None,
    crnn_sha256: str | None = None,
    handwriting: HandwritingConfig | None = None,
    check_numbers: str | bool = "suspect",
    convert: bool = True,
    soffice: str | Sequence[str] | None = None,
    convert_timeout: float | None = None,
    max_pages: int = MAX_PAGES,
    time_limit: float | None = None,
    tables: str | None = None,
    tables_models: str | os.PathLike[str] | None = None,
) -> Document:
    """Read a document and return its ``heocr_document.v1`` record.

    ``source`` is a file path or the file's bytes. The format is decided from the
    content, never from the file name, and the file name is not recorded.

    ``repairs=False`` turns every repair rule off (``heocr.repairs``): the output is then
    the extracted text, byte for byte.

    ``ocr`` runs the OCR route on pages and regions that need it, through stock Tesseract
    when it is installed (``heocr.ocr.tesseract``): ``ocr_lang`` is ``heb`` by default,
    ``ocr_psm`` is 3, and ``tesseract`` names the program when it is not on the PATH.
    ``ocr_model`` requests HeOCR's modern-print model, ``heocr_modern_v1``, by default. The GitHub
    install has no bundled models. If you have local model files, ``"oldprint"`` requests
    ``heocr_oldprint_v1``, and
    ``"auto"`` picks one of the two per document from the route table (``heocr.ocr.routes``), with a probe
    of a few lines, recorded in each OCR block's ``ocr.model_route``. The default and ``auto`` apply when
    ``ocr_lang`` is ``heb``; with another ``ocr_lang``, stock Tesseract reads with it. When a bundled model's
    file is missing (an install from the repository), stock Tesseract reads instead and the document is
    flagged ``bundled_model_unavailable``. ``ocr_model="heb"`` (or ``"stock"``, or ``None``) reads with stock
    Tesseract ``ocr_lang``. A local ``.traineddata`` path runs that model instead of ``ocr_lang``.
    Without Tesseract, or with ``ocr=False``, those pages are recorded as unread.
    ``ocr_capture_type`` opts into a single model for a declared capture. ``phone``, ``iphone`` and
    ``pixel`` use modern_v1; ``scanner`` and ``issuer-render`` use a pinned local Rivok Full model.
    This route needs Hebrew Tesseract OCR and reports unread OCR when its model is unavailable.
    ``ocr_router="v2"`` selects the capture route using a declaration when supplied, PDF rendering
    for document OCR, or exposure/focal-length metadata for JPEG photos. Inputs without a signal
    use the usual model. The option leaves text-layer handling unchanged.
    ``ocr_layout="v1"`` enables whole-page OCR column ordering, additional table
    evidence, logical mixed-script runs and guarded padding of inked crop edges.
    ``ocr_old_print_model`` adds a historical-print probe to router v2. It takes the local
    pinned P-hist v2 ``.traineddata`` file and uses it only on OCR pages with historical evidence.
    ``ocr_hybrid="haiku"`` consents to sending whole-page OCR images to Anthropic.
    It requires the ``ANTHROPIC_API_KEY`` environment variable for hosted reading.
    ``ocr_hybrid_old_print=True`` also sends the pinned local R8b OCR draft for correction.
    ``ocr_hybrid_confidence`` switches to local-first router v2 with hosted correction
    of only the existing prose lines below the provided local score (0 to 100).
    It sends the selected crops and their local OCR.
    ``ocr_hybrid_max_usd`` caps API list-price spend per document (default USD 1.00).
    The configured local OCR route handles pages where hosted reading is unavailable.
    ``ocr_phone_geometry=True`` enables perspective-corrected line OCR on phone images with
    supported page edges. Use a capture declaration or camera JPEG metadata.
    ``profile="phone-specialist"`` activates capture routing, layout v1 and phone
    geometry for standalone phone images. A local ``phone_model`` and
    ``phone_model_sha256`` can replace modern v1 on rectified line crops.
    The page read still uses modern v1. The experimental
    ``profile="phone-specialist-fast"`` skips the two auxiliary page rereads and
    some high-confidence rectified line rereads. Other captures use their usual route.
    ``photo_preprocess="gray-otsu"`` sends grayscale rasters to the main Tesseract reader
    with internal Otsu thresholding on standalone photos. Use a phone capture declaration
    when camera metadata is unavailable.
    ``force_ocr`` reads every PDF page by OCR, its text layer ignored (flag ``ocr_forced``): a workaround
    for a text layer heocr reads badly. It needs ``ocr``.
    ``ocr_line_v6`` opts Rivok Full page prose into a two-model reread of the existing lines.
    It takes a local pinned v6 ``.traineddata`` file. With router v2 it runs only on Rivok Full
    page OCR prose and can be supplied for a batch with mixed input captures.
    ``ocr_line_batch=True`` batches these prose crops per model within the page.
    ``ocr_number_batch=True`` batches single-word number crops for full-page OCR
    when the extraction has no time limit.
    It needs ``ocr_line_v6`` and uses individual reads when a batch fails or a
    document time limit is set.

    ``crnn_model`` and ``crnn_sha256`` opt OCR page prose into a local CPU ONNX line reader.
    Tesseract supplies the ordered boxes and reads tables, form entries and image regions.
    This option needs Hebrew OCR and the ``crnn`` extra.
    ``handwriting`` opts image-only pages into the experimental Kraken route. Provide a local,
    SHA-256-pinned recognition model. Auto mode requires a segmenter with handwriting line tags.
    Use force mode for input known to be handwritten. Tesseract remains the fallback.

    ``check_numbers`` reads text-layer lines a second time, by OCR of their own image
    (``heocr.pdf.numbers``). ``"suspect"`` (the default) reads only lines with a reversed-looking
    digit group, which is the evidence the ``reversed_digit_groups`` repair needs. ``"tables"``
    also reads every table cell with amounts, percentages or dates. ``"all"`` reads every line
    and cell with them, and flags trustworthy disagreements. ``False`` turns the check off,
    including the second reading of numbers in OCR'd table cells.

    ``convert`` reads DOC, RTF and ODT by converting them to DOCX with LibreOffice
    (``heocr.convert``), when it is installed; ``soffice`` names the program when it is not on
    the PATH, and ``convert_timeout`` bounds each conversion (180 s by default). The conversion
    runs with a throwaway profile, and its processes are stopped on timeout. Without LibreOffice,
    or with ``convert=False``, those files are recorded as unread.

    ``max_pages`` bounds the pages (PDF pages, image frames) read from one document: 2,000 by
    default, since every page may cost an OCR run. The rest are recorded as one unread entry
    (``page_limit``) and the document is flagged ``page_limit_reached``.

    ``tables="tableformer"`` (the optional ``tables`` extra) adds learned table proposals on
    text-layer PDF pages: Docling's TableFormer proposes the structure of tables without rulings,
    and heocr fills their cells from the page's own text layer; ruled tables keep first claim.
    ``tables_models`` is the local directory holding the pinned weights (see ``heocr.tableformer``);
    nothing is downloaded.
    ``time_limit`` is a safety net, in seconds, for the whole document (conversion and OCR
    included); there is none by default, so a record does not depend on the machine's speed.
    It is checked before each page, image frame, embedded image and page of number checks: a
    step under way finishes (each Tesseract and LibreOffice call has its own timeout), and what
    is left is recorded as unread (``time_limit``), never dropped, with the document flagged
    ``time_limit_reached``. Numbers left unchecked flag ``numbers_not_checked``.
    """
    started = contract._now()
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")
    if tables is not None and (tables != "tableformer" or tables_models is None):
        raise ValueError("tables must be 'tableformer', with tables_models set to the pinned model directory")
    if time_limit is not None and (
        isinstance(time_limit, bool) or not isinstance(time_limit, (int, float)) or not math.isfinite(time_limit) or time_limit <= 0
    ):
        raise ValueError("time_limit must be a positive number of seconds, or None")
    if check_numbers not in (False, "suspect", "tables", "all"):
        raise ValueError("check_numbers must be 'suspect', 'tables', 'all' or False")
    if ocr:
        check_settings(ocr_lang, ocr_psm)
    ocr_model = MODEL_ALIASES.get(ocr_model, ocr_model) if isinstance(ocr_model, str) else ocr_model
    if ocr_router is not None and ocr_router != "v2":
        raise ValueError("ocr_router must be 'v2'")
    if ocr_layout is not None and (ocr_layout != "v1" or not ocr):
        raise ValueError("ocr_layout must be 'v1' and needs OCR")
    if ocr_old_print_model is not None and (
        ocr_router != "v2" or Path(ocr_old_print_model).suffix != ".traineddata"
    ):
        raise ValueError("ocr_old_print_model needs router v2 and a local .traineddata file")
    hybrid = None
    if ocr_hybrid is not None:
        from heocr.ocr.hybrid import HybridReader, parse_cap, parse_confidence

        if ocr_hybrid != "haiku" or not ocr:
            raise ValueError("ocr_hybrid must be 'haiku' and needs OCR")
        confidence = parse_confidence(
            ocr_hybrid_confidence, router=ocr_router, old_print=ocr_hybrid_old_print or ocr_old_print_model is not None,
        )
        hybrid = HybridReader(cap=parse_cap(ocr_hybrid_max_usd), old_print=ocr_hybrid_old_print, confidence=confidence)
    elif ocr_hybrid_old_print or ocr_hybrid_max_usd is not None or ocr_hybrid_confidence is not None:
        raise ValueError("hybrid options require ocr_hybrid='haiku'")
    if ocr_phone_geometry and (not ocr or ocr_psm != 3 or ocr_lang != DEFAULT_LANG or crnn_model is not None):
        raise ValueError("ocr_phone_geometry needs Hebrew Tesseract OCR with page mode 3")
    if photo_preprocess is not None and (photo_preprocess != "gray-otsu" or not ocr):
        raise ValueError("photo_preprocess must be 'gray-otsu' and needs OCR")
    if ocr_router is not None or ocr_capture_type is not None:
        from heocr.ocr.routes import capture_model_for

        if not ocr or ocr_lang != DEFAULT_LANG or ocr_model != DEFAULT_MODEL or crnn_model is not None or handwriting is not None:
            option = "ocr_router" if ocr_router is not None else "ocr_capture_type"
            raise ValueError(f"{option} needs Hebrew Tesseract OCR with the default model option")
        if ocr_capture_type is not None:
            ocr_model = capture_model_for(ocr_capture_type)
    if ocr_line_batch and ocr_line_v6 is None:
        raise ValueError("ocr_line_batch needs ocr_line_v6")
    if ocr_number_batch and not ocr:
        raise ValueError("ocr_number_batch needs OCR")
    if ocr_line_v6 is not None:
        from heocr.ocr.routes import RIVOK_FULL

        if ocr_router is None and (
            not ocr or ocr_lang != DEFAULT_LANG or ocr_model != RIVOK_FULL
            or crnn_model is not None or handwriting is not None
        ):
            raise ValueError("ocr_line_v6 needs Rivok Full page OCR without CRNN or handwriting")
        if Path(ocr_line_v6).suffix != ".traineddata":
            raise ValueError("ocr_line_v6 needs a local .traineddata file")
    if ocr_model in STOCK_MODELS or (ocr_model in ("auto", DEFAULT_MODEL) and ocr_lang != DEFAULT_LANG):
        ocr_model = None  # stock Tesseract: asked for, or a Hebrew model with a language list it would replace
    body = _source_bytes(source)

    if force_ocr and not ocr:
        raise ValueError("force_ocr needs OCR: it cannot be combined with ocr=False (--no-ocr)")
    if handwriting is not None and (not ocr or not isinstance(handwriting, HandwritingConfig)):
        raise ValueError("handwriting needs OCR and a HandwritingConfig")
    if (crnn_model is None) != (crnn_sha256 is None):
        raise ValueError("supply both crnn_model and crnn_sha256")
    if crnn_model is not None and (not ocr or ocr_lang != DEFAULT_LANG):
        raise ValueError("CRNN needs Hebrew OCR (ocr_lang='heb')")
    kind = identify(body)
    phone_profile = None
    classified = None
    if profile is not None or phone_model is not None or phone_model_sha256 is not None:
        from heocr.models import MODELS
        from heocr.ocr import phone_profile as specialist
        from heocr.ocr.routes import PHOTO_CAPTURES, classify_capture

        pin = specialist.model_pin(profile, phone_model, phone_model_sha256)
        if kind.format in IMAGE_FORMATS:
            capture = ocr_capture_type
            if capture is None:
                classified = classify_capture(body, kind.format)
                capture = classified[0]
            if capture in PHOTO_CAPTURES:
                if (not ocr or ocr_lang != DEFAULT_LANG or ocr_psm != 3 or ocr_model != DEFAULT_MODEL
                        or crnn_model is not None or handwriting is not None or hybrid is not None
                        or ocr_old_print_model is not None):
                    raise ValueError("phone-specialist needs local Hebrew Tesseract OCR in page mode 3 "
                                     "with the default page model")
                if pin is not None:
                    specialist.check_model(pin)
                ocr_router, ocr_layout, ocr_phone_geometry = "v2", "v1", True
                phone_profile = {
                    "name": profile,
                    "line_model_sha256": pin[1] if pin is not None else MODELS[DEFAULT_MODEL].sha256,
                }
    capture_reason = "declared_capture"
    if ocr_router is not None:
        from heocr.ocr.routes import RIVOK_FULL, classify_capture

        if ocr_capture_type is None:
            ocr_capture_type, capture_reason, model_override = classified or classify_capture(body, kind.format)
            if model_override is not None:
                ocr_model = model_override
        if ocr_model != RIVOK_FULL:
            ocr_line_v6 = None
    doc = Document(
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        format=kind.format,
        format_evidence=kind.evidence,
        options={
            "repairs": bool(repairs),
            "check_numbers": (check_numbers if check_numbers in ("suspect", "tables", "all") else False) if ocr else False,
            "ocr": {"engine": "tesseract", "lang": ocr_lang, "psm": ocr_psm, **({"model": ocr_model if ocr_model == "auto" else Path(ocr_model).name} if ocr_model else {})}
            if ocr
            else False,
        },
    )
    if hybrid is not None:
        doc.options["ocr"]["hybrid"] = hybrid.record
    if force_ocr:
        doc.options["force_ocr"] = True
    if crnn_model is not None:
        from heocr.ocr.crnn import CrnnReader

        doc._crnn = CrnnReader(crnn_model, crnn_sha256)
        doc.options["crnn"] = {"engine": "crnn", "model_sha256": doc._crnn.sha256}
    if tables is not None:
        from heocr.tableformer import TableFormerProposer

        doc._table_proposer = TableFormerProposer(tables_models)
        doc.options["tables"] = doc._table_proposer.record
    if handwriting is not None:
        doc._handwriting = HandwritingReader(handwriting)
        doc.options["handwriting"] = handwriting.provenance()
    doc._max_pages = max_pages
    doc.options["max_pages"] = max_pages
    if time_limit is not None:
        doc.options["time_limit"] = time_limit
        doc._time_limit, doc._deadline = float(time_limit), started + time_limit
    doc._ocr_settings = {"cmd": tesseract, "lang": ocr_lang, "psm": ocr_psm, "model": ocr_model} if ocr else None
    if ocr_layout is not None:
        doc._ocr_settings["layout"] = ocr_layout
        doc.options["ocr"]["layout"] = ocr_layout
    if ocr_router is not None:
        doc.options["ocr"]["router"] = {"version": ocr_router, "reason": capture_reason}
    if ocr_capture_type is not None:
        doc.options["ocr"]["capture_type"] = ocr_capture_type
        doc._ocr_settings["capture_type"] = ocr_capture_type
        doc._ocr_settings["capture_reason"] = capture_reason
    if phone_profile is not None:
        doc.options["ocr"]["profile"] = phone_profile
        if profile == specialist.PHONE_SPECIALIST_FAST:
            doc._ocr_settings["phone_fast"] = True
        if pin is not None:
            doc._ocr_settings["phone_model"] = pin
    if ocr_old_print_model is not None:
        from heocr.ocr.old_print import PHIST_V2_SHA256

        doc._ocr_settings["old_print_model"] = Path(ocr_old_print_model)
        doc.options["ocr"]["old_print_model_sha256"] = PHIST_V2_SHA256
    if ocr_line_v6 is not None:
        from heocr.ocr.line_models import V6_SHA256

        doc._ocr_settings["line_v6"] = ocr_line_v6
        doc._ocr_settings["line_batch"] = ocr_line_batch
        doc.options["ocr"]["line_v6_sha256"] = V6_SHA256
    if ocr_number_batch:
        doc._ocr_settings["number_batch"] = True
    if (photo_preprocess is not None or ocr_phone_geometry) and kind.format in IMAGE_FORMATS:
        from heocr.ocr.routes import PHOTO_CAPTURES, classify_capture

        capture = ocr_capture_type
        if capture is None:
            capture, _, _ = classify_capture(body, kind.format)
        if capture in PHOTO_CAPTURES:
            if photo_preprocess is not None:
                doc.options["ocr"]["photo_preprocess"] = photo_preprocess
                doc._ocr_settings["photo_gray_otsu"] = True
            if ocr_phone_geometry:
                doc._ocr_settings["phone_geometry"] = True
    if kind.format in CONVERSIONS:
        doc.options["convert"] = {"engine": "libreoffice", "to": CONVERSIONS[kind.format][1]} if convert else False
        doc._convert_settings = {"cmd": soffice, "timeout": convert_timeout or CONVERT_TIMEOUT} if convert else None
    reader = _readers().get(kind.format)
    if reader is None:
        doc.unread.append(
            Unread(scope="document", reason="format_not_supported", detail=f"no route for format {kind.format!r} yet")
        )
        doc.flags.append("not_read")
        return doc
    doc._hybrid = hybrid
    try:
        with _temporary_models() if phone_profile is not None else nullcontext():
            reader(body, doc)
    finally:
        doc._hybrid = None
    if repairs:
        from heocr.repairs import repair_hebrew_commas, repair_ocr_date_brackets, repair_ocr_ellipses

        repair_hebrew_commas(doc)
        repair_ocr_date_brackets(doc)
        repair_ocr_ellipses(doc)
    from heocr.ocr import call_failures

    failed = call_failures(doc)
    if failed:
        # Never silent: a failed or timed-out Tesseract call leaves its reading out wherever it happened.
        doc.flags.append("ocr_call_failed")
        doc.unread.append(
            Unread(scope="document", reason="ocr_call_failed",
                   detail=f"{failed} Tesseract call(s) failed or timed out; what they would have read is missing")
        )
    return doc


def _extract_image(source: Source, **options) -> Document:
    body = _source_bytes(source)
    if identify(body).format not in IMAGE_FORMATS:
        raise ValueError("extract_images needs standalone image inputs")
    with _temporary_models():
        return extract(body, **options)


def extract_images(sources: Iterable[Source], *, workers: int = 1, **options) -> Iterator[Document]:
    """Read standalone images in input order with up to eight workers.

    Each input uses the same settings and record format as :func:`extract`.
    Frames of a multi-page image stay in one record. Increase ``workers`` to overlap
    OCR of independent images, with at most that many inputs in flight. Exhaust the
    iterator or close it to join the workers if you stop reading early.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 through 8")
    read_image = partial(_extract_image, **options)
    if workers == 1:
        yield from map(read_image, sources)
        return
    sources = iter(sources)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = deque(executor.submit(read_image, source) for source in islice(sources, workers))
        while pending:
            yield pending.popleft().result()
            for source in islice(sources, 1):
                pending.append(executor.submit(read_image, source))
