"""``heocr.extract``: one call, any document."""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from heocr import contract
from heocr.contract import MAX_PAGES, Document, Unread
from heocr.convert import CONVERSIONS, CONVERT_TIMEOUT
from heocr.handwriting import HandwritingConfig, HandwritingReader
from heocr.identify import identify
from heocr.ocr.tesseract import DEFAULT_LANG, DEFAULT_PSM, check_settings

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
    source: str | os.PathLike[str] | bytes,
    *,
    repairs: bool = True,
    ocr: bool = True,
    ocr_lang: str = DEFAULT_LANG,
    ocr_psm: int = DEFAULT_PSM,
    tesseract: str | Sequence[str] | None = None,
    ocr_model: str | None = DEFAULT_MODEL,
    ocr_capture_type: str | None = None,
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
    ``force_ocr`` reads every PDF page by OCR, its text layer ignored (flag ``ocr_forced``): a workaround
    for a text layer heocr reads badly. It needs ``ocr``.
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
    if time_limit is not None and (
        isinstance(time_limit, bool) or not isinstance(time_limit, (int, float)) or not math.isfinite(time_limit) or time_limit <= 0
    ):
        raise ValueError("time_limit must be a positive number of seconds, or None")
    if check_numbers not in (False, "suspect", "tables", "all"):
        raise ValueError("check_numbers must be 'suspect', 'tables', 'all' or False")
    if ocr:
        check_settings(ocr_lang, ocr_psm)
    ocr_model = MODEL_ALIASES.get(ocr_model, ocr_model) if isinstance(ocr_model, str) else ocr_model
    if ocr_capture_type is not None:
        from heocr.ocr.routes import capture_model_for

        if not ocr or ocr_lang != DEFAULT_LANG or ocr_model != DEFAULT_MODEL or crnn_model is not None or handwriting is not None:
            raise ValueError("ocr_capture_type needs Hebrew Tesseract OCR with the default model option")
        ocr_model = capture_model_for(ocr_capture_type)
    if ocr_model in STOCK_MODELS or (ocr_model in ("auto", DEFAULT_MODEL) and ocr_lang != DEFAULT_LANG):
        ocr_model = None  # stock Tesseract: asked for, or a Hebrew model with a language list it would replace
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

    if force_ocr and not ocr:
        raise ValueError("force_ocr needs OCR: it cannot be combined with ocr=False (--no-ocr)")
    if handwriting is not None and (not ocr or not isinstance(handwriting, HandwritingConfig)):
        raise ValueError("handwriting needs OCR and a HandwritingConfig")
    if (crnn_model is None) != (crnn_sha256 is None):
        raise ValueError("supply both crnn_model and crnn_sha256")
    if crnn_model is not None and (not ocr or ocr_lang != DEFAULT_LANG):
        raise ValueError("CRNN needs Hebrew OCR (ocr_lang='heb')")
    kind = identify(body)
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
    if force_ocr:
        doc.options["force_ocr"] = True
    if crnn_model is not None:
        from heocr.ocr.crnn import CrnnReader

        doc._crnn = CrnnReader(crnn_model, crnn_sha256)
        doc.options["crnn"] = {"engine": "crnn", "model_sha256": doc._crnn.sha256}
    if handwriting is not None:
        doc._handwriting = HandwritingReader(handwriting)
        doc.options["handwriting"] = handwriting.provenance()
    doc._max_pages = max_pages
    doc.options["max_pages"] = max_pages
    if time_limit is not None:
        doc.options["time_limit"] = time_limit
        doc._time_limit, doc._deadline = float(time_limit), started + time_limit
    doc._ocr_settings = {"cmd": tesseract, "lang": ocr_lang, "psm": ocr_psm, "model": ocr_model} if ocr else None
    if ocr_capture_type is not None:
        doc.options["ocr"]["capture_type"] = ocr_capture_type
        doc._ocr_settings["capture_type"] = ocr_capture_type
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
    reader(body, doc)
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
