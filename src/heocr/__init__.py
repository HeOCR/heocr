"""heocr: Hebrew text out of any document, through the best open pipeline for it."""

__version__ = "0.3.0a2"

from heocr.api import extract  # noqa: E402
from heocr.contract import SCHEMA_ID, ContractError, Document, validate  # noqa: E402
from heocr.handwriting import HandwritingConfig  # noqa: E402


def write_docx(doc: Document, path) -> None:
    """Write ``doc`` as a Word file (DOCX) at ``path``: right-to-left paragraphs, headings in Word's
    heading styles, list labels as read, real tables, a page break between pages, and the notes,
    comments, headers and footers in their own sections at the end."""
    from pathlib import Path

    Path(path).write_bytes(doc.to_docx())


__all__ = ["SCHEMA_ID", "ContractError", "Document", "HandwritingConfig", "__version__", "extract", "validate", "write_docx"]
