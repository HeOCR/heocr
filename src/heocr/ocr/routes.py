"""OCR model routes for print-domain probing and capture classification.

Each domain names a released model (``heocr.models.MODELS``) or ``None`` for stock Tesseract ``heb``.
The table comes from evaluation's default-model study. ``auto`` is opt-in: the default reads every
document with the modern-print model (``heocr.api.DEFAULT_MODEL``).
"""

from __future__ import annotations

import re
from io import BytesIO

DOMAINS = ("modern", "old")  # modern print, and old or rabbinic print (decision 0018)
DEFAULT_DOMAIN = "modern"  # the fallback when the probe cannot tell (D6 note, cascade step 3)
# From evaluation's default-model study (2026-11-19): the office-print model for modern print, the
# P-hist pick for old print.
ROUTES: dict[str, str | None] = {"modern": "heocr_modern_v1", "old": "heocr_oldprint_v1"}

# D2e3 uses a single model per input based on capture metadata supplied by the caller.
RIVOK_FULL = "heb_rivok"
FAST_MIX_V1 = "fast-mix-v1"
PHOTO_CAPTURES = ("phone", "iphone", "pixel", "camera-photo")
CAPTURE_MODELS = {
    "phone": "heocr_modern_v1",
    "iphone": "heocr_modern_v1",
    "pixel": "heocr_modern_v1",
    "scanner": RIVOK_FULL,
    "issuer-render": RIVOK_FULL,
}


def capture_model_for(capture_type: str) -> str:
    """Select a model for an explicitly declared capture."""
    if not isinstance(capture_type, str) or capture_type not in CAPTURE_MODELS:
        raise ValueError(f"ocr_capture_type must be one of: {', '.join(CAPTURE_MODELS)}")
    return CAPTURE_MODELS[capture_type]


def classify_capture(body: bytes, file_format: str) -> tuple[str | None, str, str | None]:
    """Return the capture label, evidence and model override for the opt-in reader."""
    if file_format == "pdf":
        # The PDF reader already decides if a page or region needs OCR.
        return "pdf-render", "pdf_container", RIVOK_FULL
    if file_format in ("jpeg", "png"):
        from PIL import ExifTags, Image, ImageOps

        from heocr.ocr.phone_geometry import _sheet
        from heocr.ocr.regions import MAX_RENDER_PIXELS

        try:
            with Image.open(BytesIO(body)) as image:
                if file_format == "jpeg":
                    photo = image.getexif().get_ifd(ExifTags.IFD.Exif)
                    if photo.get(ExifTags.Base.ExposureTime) or photo.get(ExifTags.Base.FocalLength):
                        return "camera-photo", "camera_exif", CAPTURE_MODELS["phone"]
                if image.mode == "RGB" and image.width * image.height <= MAX_RENDER_PIXELS:
                    gray = ImageOps.exif_transpose(image.convert("L"))
                    if _sheet(gray) is not None:
                        return "camera-photo", "camera_perspective", CAPTURE_MODELS["phone"]
        except (OSError, ValueError, Image.DecompressionBombError):
            pass  # The image reader handles decode errors and unread reporting.
    return None, "no_capture_evidence", None


def model_for(domain: str) -> str | None:
    """The model a domain routes to (a released model's name), or None for stock ``heb``."""
    if domain not in ROUTES:
        raise ValueError(f"unknown print domain {domain!r}; known: {', '.join(DOMAINS)}")
    return ROUTES[domain]


def candidates() -> dict[str, str | None]:
    """The distinct models ``auto`` may choose between, by domain."""
    return dict(ROUTES)


# The probe (D6 note, cascade step 2): lines of the first OCR pages, read by the fallback model as the page
# is read, are read again by each other candidate; the best-scoring model reads the rest of the document.
PROBE_LINES = 8  # lines sampled at most
PROBE_PAGES = 3  # OCR pages sampled at most, before deciding with what there is
PROBE_MIN_WORDS = 3  # a sampled line has at least this many Hebrew words
PROBE_MARGIN = 0.05  # another model must beat the fallback's score by this much

_HEBREW_WORD = re.compile(r"[א-ת]{2,}")


def _start(doc) -> dict:
    models = {model_for(d) for d in DOMAINS}
    if len(models) == 1:
        return {"decided": True, "domain": DEFAULT_DOMAIN, "model": model_for(DEFAULT_DOMAIN), "reason": "single_candidate"}
    return {"decided": False, "crops": [], "pages": 0}


def current(doc) -> tuple[str | None, dict]:
    """(model, route record) for the document now: the decision, or the fallback while sampling."""
    if doc._route is None:
        doc._route = _start(doc)
    state = doc._route
    if state["decided"]:
        record = {"mode": "auto", "domain": state["domain"], "reason": state["reason"]}
        if "scores" in state:
            record["scores"] = state["scores"]
        return state["model"], record
    return model_for(DEFAULT_DOMAIN), {"mode": "auto", "domain": DEFAULT_DOMAIN, "reason": "before_decision"}


def pending(doc) -> bool:
    """Whether ``ocr_model="auto"`` is still sampling lines for its probe."""
    settings = doc._ocr_settings or {}
    if settings.get("model") != "auto":
        return False
    if doc._route is None:
        doc._route = _start(doc)
    return not doc._route["decided"]


def observe(doc, image, result, dpi: int, last_page: bool = False) -> bool:
    """Take a page's OCR lines (read by the fallback model) into the probe, and decide once enough are in,
    or at the document's last page. True when the decision switched the document to another model."""
    state = doc._route
    state["pages"] += 1
    for line in result.lines:
        if len(state["crops"]) >= PROBE_LINES:
            break
        if len(_HEBREW_WORD.findall(line.text)) >= PROBE_MIN_WORDS:
            x0, y0, x1, y1 = (int(v) for v in line.bbox)
            state["crops"].append((image.crop((max(0, x0 - 2), max(0, y0 - 2), x1 + 2, y1 + 2)), line.text, list(line.confidences)))
    if len(state["crops"]) >= PROBE_LINES or state["pages"] >= PROBE_PAGES or last_page:
        _decide(doc, dpi)
        return doc._route["model"] != model_for(DEFAULT_DOMAIN)
    return False


def _score(texts: list[str], confidences: list[float]) -> float:
    """Higher is better: mean word confidence (0-1) less the share of misspelt words (heocr.pdf.reader.misspelt_share)."""
    from heocr.pdf.reader import misspelt_share

    usable = [c for c in confidences if c >= 0]
    confidence = sum(usable) / len(usable) / 100 if usable else 0.0
    return round(confidence - (misspelt_share(" ".join(texts)) or 0.0), 4)


def _decide(doc, dpi: int) -> None:
    from heocr.ocr import OcrUnavailable, engine_for_model

    state = doc._route
    fallback = model_for(DEFAULT_DOMAIN)
    if not state["crops"]:
        doc._route = {"decided": True, "domain": DEFAULT_DOMAIN, "model": fallback, "reason": "probe_no_lines"}
        return
    scores = {DEFAULT_DOMAIN: _score([t for _, t, _ in state["crops"]], [c for _, _, cs in state["crops"] for c in cs])}
    models = {DEFAULT_DOMAIN: fallback}
    for domain in DOMAINS:
        model = model_for(domain)
        if domain == DEFAULT_DOMAIN or model == fallback or model in models.values():
            continue
        engine = engine_for_model(doc, model)
        if engine is None:
            continue
        texts, confidences = [], []
        try:
            for crop, _, _ in state["crops"]:
                read = engine.recognize(crop, dpi=dpi, psm=7)
                texts.append(" ".join(line.text for line in read.lines))
                confidences.extend(c for line in read.lines for c in line.confidences)
        except OcrUnavailable:
            continue
        scores[domain], models[domain] = _score(texts, confidences), model
    best = max(scores, key=lambda d: scores[d])
    if best != DEFAULT_DOMAIN and scores[best] - scores[DEFAULT_DOMAIN] >= PROBE_MARGIN:
        doc._route = {"decided": True, "domain": best, "model": models[best], "reason": "probe", "scores": scores}
    else:
        doc._route = {"decided": True, "domain": DEFAULT_DOMAIN, "model": fallback, "reason": "probe_unsure", "scores": scores}
