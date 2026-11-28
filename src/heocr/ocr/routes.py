"""Bounded per-document OCR model selection and capture classification."""

from __future__ import annotations

import re
import unicodedata
from io import BytesIO

from heocr.contract import time_up

DEFAULT_DOMAIN = "modern-documents"
ROUTES: dict[str, str | None] = {
    DEFAULT_DOMAIN: "heocr_modern_measured",
    "old-print": "heocr_phist_measured",
    "modern-literature": "heocr_literature",
    "pointed": "heocr_pointed",
}
DOMAINS = tuple(ROUTES)

# D2e3 uses a single model per input based on capture metadata supplied by the caller.
RIVOK_FULL = "heb_rivok"
FAST_MIX_V1 = "fast-mix-v1"
FAST_MIX_V2 = "fast-mix-v2"
FAST_MIX_V3 = "fast-mix-v3"
FAST_MIX_DOC_V1 = "fast-mix-doc-v1"
FAST_MIX_TABLE_PROFILES = (FAST_MIX_V3, FAST_MIX_DOC_V1)
FAST_MIX_PROFILES = (FAST_MIX_V1, FAST_MIX_V2, *FAST_MIX_TABLE_PROFILES)
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


# Reuse the first page OCR for geometry and the default-model scores.
PROBE_LINES = 4
PROBE_PAGES = 3
PROBE_MIN_WORDS = 3
PROBE_MARGIN = 0.05
POINTED_MIN_LETTERS = 30
POINTED_MIN_DENSITY = 0.20  # selected on pointed and unpointed DEV probes

_HEBREW_WORD = re.compile(r"[א-ת]{2,}")
_MARKS = r"֑-ׇֽֿׁׂׅׄ"
_HEBREW_MARK = re.compile(rf"[{_MARKS}]")
_LETTER_AND_MARKS = re.compile(rf"[א-ת][{_MARKS}]*")
# The vowels and meteg distinguish sustained pointing. Isolated dagesh,
# shin/sin dots, cantillation and Hebrew punctuation do not.
_POINT = re.compile(r"[ְ-ׇֻֽ]")


def _plain(text: str) -> str:
    return _HEBREW_MARK.sub("", unicodedata.normalize("NFKD", text))


def point_density(text: str) -> tuple[int, float]:
    """Return the Hebrew base-letter count and fraction with vowel marks."""
    text = unicodedata.normalize("NFKD", text)
    # Count only marks on a Hebrew base, and at most once per letter.
    groups = _LETTER_AND_MARKS.findall(text)
    return len(groups), sum(bool(_POINT.search(group)) for group in groups) / len(groups) if groups else 0.0


def _decision(domain: str, reason: str, **evidence) -> dict:
    return {"decided": True, "domain": domain, "model": model_for(domain), "reason": reason, **evidence}


def _start() -> dict:
    if len(set(ROUTES.values())) == 1:
        return _decision(DEFAULT_DOMAIN, "single_candidate")
    return {"decided": False, "crops": [], "pages": 0}


def current(doc) -> tuple[str | None, dict]:
    """(model, route record) for the decision or the default while sampling."""
    if doc._route is None:
        doc._route = _start()
    state = doc._route
    if state["decided"]:
        record = {"mode": "auto", **{key: value for key, value in state.items() if key != "decided"}}
        return state["model"], record
    model = model_for(DEFAULT_DOMAIN)
    return model, {"mode": "auto", "domain": DEFAULT_DOMAIN, "model": model, "reason": "before_decision"}


def pending(doc) -> bool:
    """Whether the document's automatic OCR route still needs probe evidence."""
    if (doc._ocr_settings or {}).get("model") != "auto":
        return False
    if doc._route is None:
        doc._route = _start()
    return not doc._route["decided"]


def born_digital(doc) -> None:
    """Use the document model for native formats or a visible native PDF layer."""
    if ((doc._ocr_settings or {}).get("model") == "auto"
            and (doc._route is None or not (doc._route["decided"] or doc._route["pages"]))):
        doc._route = _decision(DEFAULT_DOMAIN, "born_digital")


def manual_record(model: str) -> dict:
    for domain, routed in ROUTES.items():
        if model == routed:
            return {"mode": "fixed", "domain": domain, "model": model, "reason": "manual_override"}
    return {"mode": "fixed", "model": model}


def finish(doc) -> None:
    """Write the document-level decision, including documents read without OCR."""
    model = (doc._ocr_settings or {}).get("model")
    if model == "auto":
        if pending(doc):
            reason = "probe_incomplete" if doc._route["crops"] else "probe_no_lines"
            doc._route = _decision(DEFAULT_DOMAIN, reason)
        _, route = current(doc)
        previous = doc.options["ocr"].get("model_route", {})
        if previous.get("domain") == route["domain"]:
            route.update({key: previous[key] for key in ("model", "unavailable") if key in previous})
        doc.options["ocr"]["model_route"] = route
    elif model in ROUTES.values() and model is not None:
        route = manual_record(model)
        if doc._ocr_error and doc._ocr_engine is None:
            route.update(model=None, unavailable=model)
        doc.options["ocr"]["model_route"] = route


def observe(doc, image, result, dpi: int, last_page: bool = False) -> bool:
    """Keep a bounded sample. Return true when the chosen model differs from the default."""
    state = doc._route
    state["pages"] += 1
    for line in result.lines:
        if len(state["crops"]) >= PROBE_LINES:
            break
        if len(_HEBREW_WORD.findall(_plain(line.text))) >= PROBE_MIN_WORDS:
            x0, y0, x1, y1 = (int(v) for v in line.bbox)
            crop = image.crop((max(0, x0 - 2), max(0, y0 - 2), x1 + 2, y1 + 2))
            state["crops"].append((crop, dpi, line.text, line.confidences))
    if len(state["crops"]) >= PROBE_LINES or state["pages"] >= PROBE_PAGES or last_page:
        _decide(doc)
        return doc._route["model"] != model_for(DEFAULT_DOMAIN)
    return False


def _score(texts: list[str], confidences: list[float]) -> float:
    """Mean word confidence minus the share of misspelt Hebrew words."""
    from heocr.pdf.reader import misspelt_share

    usable = [c for c in confidences if c >= 0]
    confidence = sum(usable) / len(usable) / 100 if usable else 0.0
    return round(confidence - (misspelt_share(_plain(" ".join(texts))) or 0.0), 4)


def _reread(doc, model, crops):
    from heocr.ocr import OcrUnavailable, engine_for_model

    if time_up(doc) or (engine := engine_for_model(doc, model)) is None:
        return None
    texts, confidences = [], []
    for crop, dpi, _, _ in crops:
        if time_up(doc):
            return None
        try:
            result = engine.recognize(crop, dpi=dpi, psm=7)
        except OcrUnavailable:
            return None
        texts.extend(line.text for line in result.lines)
        confidences.extend(c for line in result.lines for c in line.confidences)
    return texts, confidences


def _decide(doc) -> None:
    crops = doc._route["crops"]
    if not crops:
        doc._route = _decision(DEFAULT_DOMAIN, "probe_no_lines")
        return
    evidence = {}
    unavailable = []
    pointed = _reread(doc, model_for("pointed"), crops)
    if pointed is None:
        unavailable.append("pointed")
    else:
        letters, density = point_density(" ".join(pointed[0]))
        evidence = {"niqqud_letters": letters, "niqqud_density": round(density, 4)}
        if letters >= POINTED_MIN_LETTERS and density >= POINTED_MIN_DENSITY:
            doc._route = _decision("pointed", "probe_niqqud", **evidence)
            return

    scores = {DEFAULT_DOMAIN: _score([text for _, _, text, _ in crops], [c for _, _, _, cs in crops for c in cs])}
    for domain in ("old-print", "modern-literature"):
        model = model_for(domain)
        if model == model_for(DEFAULT_DOMAIN):
            continue
        sample = _reread(doc, model, crops)
        if sample is None:
            unavailable.append(domain)
        elif len(_HEBREW_WORD.findall(_plain(" ".join(sample[0])))) >= PROBE_MIN_WORDS:
            scores[domain] = _score(*sample)
    if unavailable:
        evidence["probe_unavailable"] = unavailable
    best = max(scores, key=scores.get)
    if best != DEFAULT_DOMAIN and round(scores[best] - scores[DEFAULT_DOMAIN], 4) >= PROBE_MARGIN:
        doc._route = _decision(best, "probe", scores=scores, **evidence)
    else:
        doc._route = _decision(DEFAULT_DOMAIN, "probe_unsure", scores=scores, **evidence)
