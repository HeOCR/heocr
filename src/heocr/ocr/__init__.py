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
    """The document's OCR engine, found once; None when OCR is off or unavailable (reason kept).

    With ``ocr_model="auto"`` it is the engine of the model the route table and the probe chose for the
    document (``heocr.ocr.routes``), or the fallback domain's while the probe is still sampling."""
    settings = doc._ocr_settings
    if settings is None:
        return None
    if settings.get("model") == "auto":
        from heocr.ocr import routes

        model, route = routes.current(doc)
        engine = engine_for_model(doc, model)
        if engine is not None:
            engine.route = route
        return engine
    if doc._ocr_engine is None and doc._ocr_error is None:
        try:
            doc._ocr_engine = Tesseract.find(
                settings["cmd"], lang=settings["lang"], psm=settings["psm"], model=settings.get("model")
            )
            doc.components.update(doc._ocr_engine.provenance())
        except OcrUnavailable as exc:
            doc._ocr_error = str(exc)
    return doc._ocr_engine


def engine_for_model(doc: "Document", model: str | None) -> Tesseract | None:
    """The engine for one model (None: stock ``lang``), found once per document; None when unavailable."""
    settings = doc._ocr_settings
    if model in doc._ocr_engines:
        return doc._ocr_engines[model]
    try:
        engine = Tesseract.find(settings["cmd"], lang=settings["lang"], psm=settings["psm"], model=model)
        doc.components.update(engine.provenance())
    except OcrUnavailable as exc:
        doc._ocr_error = doc._ocr_error or str(exc)
        engine = None
    doc._ocr_engines[model] = engine
    if doc._ocr_engine is None:
        doc._ocr_engine = engine
    return engine


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
        **({"model_route": dict(engine.route)} if getattr(engine, "route", None) else {}),
    }


from heocr.ocr import routes  # noqa: E402  (after engine_for_model, which it imports lazily)

__all__ = ["NUMBER_LANG", "OcrUnavailable", "Tesseract", "block_ocr", "engine_for", "engine_for_model", "number_engine_for", "routes", "unavailable_reason"]
