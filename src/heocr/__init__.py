"""heocr: Hebrew text out of any document, through the best open pipeline for it."""

__version__ = "0.3.0"

from heocr.api import extract, extract_images  # noqa: E402
from heocr.contract import SCHEMA_ID, ContractError, Document, validate  # noqa: E402
from heocr.handwriting import HandwritingConfig  # noqa: E402


def write_docx(doc: Document, path) -> None:
    """Write ``doc`` as a Word file (DOCX) at ``path``: right-to-left paragraphs, headings in Word's
    heading styles, list labels as read, real tables, a page break between pages, and the notes,
    comments, headers and footers in their own sections at the end."""
    from pathlib import Path

    Path(path).write_bytes(doc.to_docx())


def write_xlsx(doc: Document, path) -> None:
    """Write the tables of ``doc`` as an Excel workbook at ``path``: a sheet per table, every position as
    read (text, never reparsed), merges for spans, right-to-left sheets for right-to-left tables."""
    from pathlib import Path

    Path(path).write_bytes(doc.to_xlsx())


__all__ = ["SCHEMA_ID", "ContractError", "Document", "HandwritingConfig", "__version__", "extract", "extract_images", "validate", "write_docx",
           "write_xlsx"]
