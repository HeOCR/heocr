"""Conservative historical Hebrew print branch of capture router v2."""

from __future__ import annotations

import re

from heocr.contract import time_up
from heocr.ocr.tesseract import OcrResult, OcrUnavailable, Tesseract

PHIST_V2_SHA256 = "240716ac4c253d264c1fa62d81c207e1313fda59a99ae79c1e21f7f0509feded"
PHIST_V2_SIZE = 3704077
MODEL_LABEL = "phist_v2"
_KNOWN_MODEL = ("old-print", PHIST_V2_SHA256)

_HEBREW = re.compile(r"[א-ת]")
_CUE = re.compile(
    r"\b(?:רבי|רבינו|רבנו|רבנן|שנאמר|שנינו|דכתיב|תלמוד|מדרש|משנה|גמרא|בבלי|ירושלמי|"
    r"מכילתא|תוספתא|במקרא|וגו|רבותינו|חכמים|אמרו|הכתוב|מנין|לאמר|ויהי|ויאמר)\b"
)


def _evidence(result: OcrResult) -> tuple[set[str], int, int]:
    cues: set[str] = set()
    cue_lines = 0
    hebrew_count = 0
    digit_count = 0
    for line in result.lines:
        hits = set(_CUE.findall(line.text))
        if hits:
            cue_lines += 1
            cues.update(hits)
        hebrew_count += len(_HEBREW.findall(line.text))
        digit_count += sum(char.isdecimal() for char in line.text)
    if cue_lines < 2:
        cues.clear()
    return cues, hebrew_count, digit_count


def _engine(doc) -> Tesseract | None:
    if _KNOWN_MODEL not in doc._ocr_engines:
        settings = doc._ocr_settings
        path = settings["old_print_model"]
        try:
            if not path.is_file() or path.stat().st_size != PHIST_V2_SIZE:
                raise OcrUnavailable("invalid pinned P-hist v2 model")
            engine = Tesseract.find(settings["cmd"], psm=settings["psm"],
                                    local_pin=(path, MODEL_LABEL, PHIST_V2_SHA256))
        except (OcrUnavailable, OSError):
            doc.flags.append("ocr_old_print_model_unavailable")
            engine = None
        else:
            engine.route = {"mode": "historical_print", "domain": "old", "model": MODEL_LABEL,
                            "model_sha256": PHIST_V2_SHA256, "reason": "classical_print_evidence"}
        doc._ocr_engines[_KNOWN_MODEL] = engine
    return doc._ocr_engines[_KNOWN_MODEL]


def read_page(doc, image, dpi: int, primary: OcrResult) -> tuple[Tesseract, OcrResult] | None:
    """Keep router v2 unless both recognizers find sufficient old-print cues."""
    cues, hebrew, digits = _evidence(primary)
    primary_confidence = primary.mean_confidence
    if (len(cues) < 2 or hebrew < 120 or digits * 100 > hebrew
            or primary_confidence is None or time_up(doc)):
        return None
    reader = _engine(doc)
    if reader is None or time_up(doc):
        return None
    try:
        alternative = reader.recognize(image, dpi=dpi)
    except OcrUnavailable:
        doc.flags.append("ocr_old_print_probe_failed")
        return None
    other_cues, other_hebrew, other_digits = _evidence(alternative)
    confidence = alternative.mean_confidence
    if (len(cues & other_cues) < 2 or other_digits * 100 > other_hebrew
            or not 0.9 * hebrew <= other_hebrew <= 1.1 * hebrew
            or confidence is None or confidence < max(80, primary_confidence - 6)):
        return None
    doc.components["tesseract:phist_v2.traineddata"] = f"sha256:{PHIST_V2_SHA256}"
    return reader, alternative
