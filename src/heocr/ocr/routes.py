"""Which OCR model reads which print (D6): the route table for ``ocr_model="auto"``.

Each domain names a released model (``heocr.models.MODELS``) or ``None`` for stock Tesseract ``heb``.
The table is filled from evaluation's default-model study; until then every domain reads with stock
``heb``, so ``auto`` changes nothing but the record of how each page was routed. The default stays
stock ``heb`` (not ``auto``) until evaluation's numbers and a registered gate say otherwise.
"""

from __future__ import annotations

DOMAINS = ("modern", "old")  # modern print, and old or rabbinic print (decision 0018)
DEFAULT_DOMAIN = "modern"  # the fallback when the probe cannot tell (D6 note, cascade step 3)
ROUTES: dict[str, str | None] = {"modern": None, "old": None}


def model_for(domain: str) -> str | None:
    """The model a domain routes to (a released model's name), or None for stock ``heb``."""
    if domain not in ROUTES:
        raise ValueError(f"unknown print domain {domain!r}; known: {', '.join(DOMAINS)}")
    return ROUTES[domain]


def candidates() -> dict[str, str | None]:
    """The distinct models ``auto`` may choose between, by domain."""
    return dict(ROUTES)
