"""The OCR route: pages and regions whose text exists only as an image.

D2e runs stock Tesseract (``heocr.ocr.tesseract``). HeOCR's own recognizers, through kraken in
the ``[ocr]`` extra, follow in a later slice. OCR text is always marked unverified, with its
engine, settings and confidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from heocr.ocr.tesseract import OcrUnavailable, Tesseract

if TYPE_CHECKING:  # pragma: no cover
    from heocr.contract import Document


def engine_for(doc: "Document") -> Tesseract | None:
    """The document's OCR engine, found once; None when OCR is off or unavailable (reason kept)."""
    settings = doc._ocr_settings
    if settings is None:
        return None
    if doc._ocr_engine is None and doc._ocr_error is None:
        try:
            doc._ocr_engine = Tesseract.find(
                settings["cmd"], lang=settings["lang"], psm=settings["psm"], model=settings.get("model")
            )
            doc.components.update(doc._ocr_engine.provenance())
        except OcrUnavailable as exc:
            doc._ocr_error = str(exc)
    return doc._ocr_engine


NUMBER_LANG = "eng"  # stock eng reads digits far better than stock heb (dev split: 96.5% against 37% agreement)


def number_engine_for(doc: "Document") -> Tesseract | None:
    """The engine for second readings of numbers: stock ``eng`` at the same Tesseract, found once.

    Stock ``heb`` misreads isolated digits often (a table cell's "8,500,000" as "00"), so a
    number's second reading is in ``eng``. When ``eng`` is not installed, the document's own
    engine reads instead, and the document says so (``numbers_read_with_main_model``).
    """
    main = engine_for(doc)
    if main is None:
        return None
    if doc._number_engine is None and doc._number_error is None:
        try:
            doc._number_engine = Tesseract.find(doc._ocr_settings["cmd"], lang=NUMBER_LANG, psm=7)
            doc.components["tesseract:number_lang"] = NUMBER_LANG
            for language, sha in doc._number_engine.models.items():
                doc.components[f"tesseract:{language}.traineddata"] = f"sha256:{sha}"
        except OcrUnavailable as exc:
            doc._number_error = str(exc)
            if "numbers_read_with_main_model" not in doc.flags:
                doc.flags.append("numbers_read_with_main_model")
    return doc._number_engine or main


def unavailable_reason(doc: "Document") -> str:
    if doc._ocr_settings is None:
        return "ocr turned off"
    return doc._ocr_error or "ocr unavailable"


def block_ocr(engine: Tesseract, lines) -> dict:
    """The ``ocr`` object of one block: engine, settings, and its own words and mean confidence."""
    confidences = [c for line in lines for c in line.confidences if c >= 0]
    return {
        "status": "unverified",
        "engine": "tesseract",
        "lang": engine.lang,
        "psm": engine.psm,
        "words": sum(len(line.confidences) for line in lines),
        "mean_confidence": round(sum(confidences) / len(confidences), 2) if confidences else None,
    }


__all__ = ["NUMBER_LANG", "OcrUnavailable", "Tesseract", "block_ocr", "engine_for", "number_engine_for", "unavailable_reason"]
