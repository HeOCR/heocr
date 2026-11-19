"""heocr: Hebrew text out of any document, through the best open pipeline for it."""

__version__ = "0.2.0rc1"

from heocr.api import extract  # noqa: E402
from heocr.contract import SCHEMA_ID, ContractError, Document, validate  # noqa: E402

__all__ = ["SCHEMA_ID", "ContractError", "Document", "__version__", "extract", "validate"]
