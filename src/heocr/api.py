"""``heocr.extract``: one call, any document."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from heocr.contract import Document, Unread
from heocr.identify import identify
from heocr.ocr.tesseract import DEFAULT_LANG, DEFAULT_PSM, check_settings

# Inputs above this size are refused before parsing. Real tender documents are far smaller.
MAX_FILE_BYTES = 512 * 1024**2

Reader = Callable[[bytes, Document], None]


def _readers() -> dict[str, Reader]:
    """Formats with a route, mapped to the reader that fills a Document in place."""
    from heocr.docx.reader import read_docx
    from heocr.pdf.reader import read_pdf

    return {"docx": read_docx, "pdf": read_pdf}


def extract(
    source: str | os.PathLike[str] | bytes,
    *,
    repairs: bool = True,
    ocr: bool = True,
    ocr_lang: str = DEFAULT_LANG,
    ocr_psm: int = DEFAULT_PSM,
    tesseract: str | Sequence[str] | None = None,
    ocr_model: str | None = None,
    check_numbers: str | bool = "suspect",
) -> Document:
    """Read a document and return its ``heocr_document.v1`` record.

    ``source`` is a file path or the file's bytes. The format is decided from the
    content, never from the file name, and the file name is not recorded.

    ``repairs=False`` turns every repair rule off (``heocr.repairs``): the output is then
    the extracted text, byte for byte.

    ``ocr`` runs the OCR route on pages and regions that need it, through stock Tesseract
    when it is installed (``heocr.ocr.tesseract``): ``ocr_lang`` is ``heb`` by default,
    ``ocr_psm`` is 3, and ``tesseract`` names the program when it is not on the PATH.
    ``ocr_model`` runs a HeOCR traineddata instead of ``ocr_lang``: a released model's name
    (downloaded on purpose first, ``heocr.models.download``) or a ``.traineddata`` path.
    Without Tesseract, or with ``ocr=False``, those pages are recorded as unread.

    ``check_numbers`` reads text-layer lines a second time, by OCR of their own image
    (``heocr.pdf.numbers``). ``"suspect"`` (the default) reads only lines with a reversed-looking
    digit group, which is the evidence the ``reversed_digit_groups`` repair needs. ``"all"`` also
    reads every line with amounts, percentages or dates, and flags trustworthy disagreements.
    ``False`` turns the check off.
    """
    if check_numbers not in (False, "suspect", "all"):
        raise ValueError("check_numbers must be 'suspect', 'all' or False")
    if ocr:
        check_settings(ocr_lang, ocr_psm)
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

    kind = identify(body)
    doc = Document(
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        format=kind.format,
        format_evidence=kind.evidence,
        options={
            "repairs": bool(repairs),
            "check_numbers": (check_numbers if check_numbers in ("suspect", "all") else False) if ocr else False,
            "ocr": {"engine": "tesseract", "lang": ocr_lang, "psm": ocr_psm, **({"model": Path(ocr_model).name if ocr_model else None} if ocr_model else {})}
            if ocr
            else False,
        },
    )
    doc._ocr_settings = {"cmd": tesseract, "lang": ocr_lang, "psm": ocr_psm, "model": ocr_model} if ocr else None
    reader = _readers().get(kind.format)
    if reader is None:
        doc.unread.append(
            Unread(scope="document", reason="format_not_supported", detail=f"no route for format {kind.format!r} yet")
        )
        doc.flags.append("not_read")
        return doc
    reader(body, doc)
    return doc
