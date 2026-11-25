"""Local line-model pin for the opt-in phone specialist profile."""

from pathlib import Path
import re

from heocr.models import MODELS, sha256_of
from heocr.ocr.tesseract import OcrUnavailable, Tesseract

PHONE_SPECIALIST = "phone-specialist"
PHONE_SPECIALIST_FAST = "phone-specialist-fast"
PHONE_PROFILES = (PHONE_SPECIALIST, PHONE_SPECIALIST_FAST)
MODERN_MODEL = "heocr_modern_v1"
SPECIALIST_LANG = "phone_specialist"


def model_pin(profile, model, sha256):
    if profile is not None and profile not in PHONE_PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(PHONE_PROFILES)}")
    if model is None and sha256 is None:
        return None
    if profile is None or model is None or sha256 is None:
        raise ValueError("phone_model and phone_model_sha256 must be supplied together with a phone-specialist profile")
    path = Path(model)
    if path.suffix != ".traineddata" or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError("phone_model needs a local .traineddata file and a 64-digit SHA-256")
    return path, sha256.lower()


def check_model(pin):
    path, expected = pin
    try:
        matches = path.is_file() and sha256_of(path) == expected
    except OSError:
        matches = False
    if not matches:
        raise ValueError("phone model is unavailable or does not match its SHA-256")


def line_engine(doc):
    """Stage and verify the line model once, when geometry has crops to read."""
    path, sha256 = doc._ocr_settings["phone_model"]
    if PHONE_SPECIALIST not in doc._ocr_engines:
        modern = MODELS[MODERN_MODEL]
        language = modern.name if sha256 == modern.sha256 else SPECIALIST_LANG
        try:
            engine = Tesseract.find(doc._ocr_settings["cmd"], psm=3, local_pin=(path, language, sha256))
        except (OcrUnavailable, OSError):
            raise ValueError("phone model could not be staged with its SHA-256") from None
        doc._ocr_engines[PHONE_SPECIALIST] = engine
        doc.components["phone_specialist:line_model_sha256"] = f"sha256:{sha256}"
    return doc._ocr_engines[PHONE_SPECIALIST]
