"""``heocr.extract``: one call, any document."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from heocr.contract import Document, Unread
from heocr.identify import identify

# Inputs above this size are refused before parsing. Real tender documents are far smaller.
MAX_FILE_BYTES = 512 * 1024**2

Reader = Callable[[bytes, Document], None]


def _readers() -> dict[str, Reader]:
    """Formats with a route, mapped to the reader that fills a Document in place."""
    from heocr.docx.reader import read_docx
    from heocr.pdf.reader import read_pdf

    return {"docx": read_docx, "pdf": read_pdf}


def extract(source: str | os.PathLike[str] | bytes, *, repairs: bool = True) -> Document:
    """Read a document and return its ``heocr_document.v1`` record.

    ``source`` is a file path or the file's bytes. The format is decided from the
    content, never from the file name, and the file name is not recorded.

    ``repairs=False`` turns every repair rule off (``heocr.repairs``): the output is then
    the extracted text, byte for byte.
    """
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
        options={"repairs": bool(repairs)},
    )
    reader = _readers().get(kind.format)
    if reader is None:
        doc.unread.append(
            Unread(scope="document", reason="format_not_supported", detail=f"no route for format {kind.format!r} yet")
        )
        doc.flags.append("not_read")
        return doc
    reader(body, doc)
    return doc
