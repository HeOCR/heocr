"""Opt-in recognition of existing OCR prose lines with Rivok Full and v6."""

from __future__ import annotations

import re
from itertools import islice

from heocr.contract import time_up
from heocr.ocr.crnn import line_box
from heocr.ocr.tesseract import OcrResult, OcrUnavailable, Tesseract
from heocr.textflags import text_flags

V6_SHA256 = "52b94ef13eaa3c8d32403ab05e12fc9be74d22a2d1aaa4d2de904b313b0e1609"
MIN_CONFIDENCE = 80
V6_MARGIN = 10
_MAX_BATCH_LINES = 64


def _line_reading(result: OcrResult) -> tuple[str, float] | None:
    confidence = result.mean_confidence
    if len(result.lines) != 1 or confidence is None:
        return None
    return result.lines[0].text, confidence


def _read_line(engine: Tesseract, crop, dpi: int) -> tuple[str, float] | None:
    try:
        return _line_reading(engine.recognize(crop, dpi=dpi, psm=7))
    except OcrUnavailable:
        return None


def choose_line(original: str, rivok: tuple[str, float], v6: tuple[str, float]) -> tuple[str, str] | None:
    """Use a corroborated reread or a clearly stronger v6 line, preserving digits."""
    rivok_text, rivok_confidence = rivok
    v6_text, v6_confidence = v6
    if min(rivok_confidence, v6_confidence) >= MIN_CONFIDENCE and rivok_text.split() == v6_text.split():
        text, model = rivok_text, "rivok_agreement"
    elif v6_confidence >= MIN_CONFIDENCE and v6_confidence - rivok_confidence > V6_MARGIN:
        text, model = v6_text, "v6_confidence"
    else:
        return None
    old_size = len("".join(original.split()))
    new_size = len("".join(text.split()))
    if (text == original or not text.strip() or not 0.85 * old_size <= new_size <= 1.2 * old_size
            or re.findall(r"\d+", text) != re.findall(r"\d+", original)):
        return None
    return text, model


def _v6_engine(doc) -> Tesseract | None:
    key = ("line_v6", V6_SHA256)
    if key in doc._ocr_engines:
        return doc._ocr_engines[key]
    settings = doc._ocr_settings
    try:
        engine = Tesseract.find(settings["cmd"], model=settings["line_v6"], psm=7)
        if engine.models != {engine.lang: V6_SHA256}:
            raise OcrUnavailable("line v6 SHA-256 mismatch")
    except OcrUnavailable:
        engine = None
        doc.flags.append("ocr_line_v6_unavailable")
    else:
        doc.components["tesseract:line_v6.traineddata"] = f"sha256:{V6_SHA256}"
    doc._ocr_engines[key] = engine
    return engine


def _crop_line(image, box):
    from PIL import ImageOps

    return ImageOps.expand(image.crop(box).convert("L"), 10, 255)


def _batched_readings(prose, image, scale, dpi, rivok, v6):
    """Read bounded groups and leave failed groups for the existing serial path."""
    boxes = (
        ((block_index, line_index), line_box(image, line.bbox, scale))
        for block_index, block in enumerate(prose)
        for line_index, line in enumerate(block.lines, start=1)
    )
    slots = ((key, box) for key, box in boxes if box[0] < box[2] and box[1] < box[3])
    readings = {}
    while chunk := list(islice(slots, _MAX_BATCH_LINES)):
        if len(chunk) == 1:
            break
        try:
            first = rivok.recognize_line_batch((_crop_line(image, box) for _, box in chunk), dpi)
            primary = {key: _line_reading(result) for (key, _), result in zip(chunk, first, strict=True)}
            eligible = [(key, box) for key, box in chunk if primary[key] is not None]
            second = v6.recognize_line_batch((_crop_line(image, box) for _, box in eligible), dpi)
            secondary = {key: _line_reading(result) for (key, _), result in zip(eligible, second, strict=True)}
        except (OcrUnavailable, OSError, ValueError):
            continue
        readings.update((key, (reading, secondary.get(key))) for key, reading in primary.items())
    return readings


def read_page_lines(doc, blocks, image, scale: float, rivok: Tesseract) -> int:
    """Reread the already assembled prose slots without changing their geometry or order."""
    prose = [block for block in blocks if block.kind == "text" and block.source == "ocr" and block.lines]
    if not prose or time_up(doc):
        return 0
    v6 = _v6_engine(doc)
    if v6 is None:
        return 0
    dpi = round(scale * 72)
    batched = _batched_readings(prose, image, scale, dpi, rivok, v6) if (
        doc._ocr_settings.get("line_batch") and doc._time_limit is None
    ) else {}
    difference = 0
    for block_index, block in enumerate(prose):
        original = block.text
        changes = []
        fallback = False
        for index, line in enumerate(block.lines, start=1):
            if time_up(doc):
                fallback = True
                break
            pair = batched.get((block_index, index))
            if pair is None:
                x0, y0, x1, y1 = line_box(image, line.bbox, scale)
                if x0 >= x1 or y0 >= y1:
                    fallback = True
                    continue
                crop = _crop_line(image, (x0, y0, x1, y1))
                first = _read_line(rivok, crop, dpi)
                second = _read_line(v6, crop, dpi) if first is not None and not time_up(doc) else None
            else:
                first, second = pair
            if first is None or second is None:
                fallback = True
                continue
            chosen = choose_line(line.text, first, second)
            if chosen is not None:
                line.text, model = chosen
                changes.append({"line": index, "selection": model})
        if changes:
            block.text = "\n".join(line.text for line in block.lines)
            difference += len("".join(block.text.split())) - len("".join(original.split()))
            stale = set(text_flags(original)) | {"numbers_from_second_reading", "latin_from_second_reading"}
            block.flags[:] = [flag for flag in block.flags if flag not in stale] + text_flags(block.text)
            block.ocr.update(line_router={"psm": 7, "changes": changes},
                             words=len(block.text.split()), mean_confidence=None)
            block.checks = None
        if fallback:
            block.flags.append("ocr_line_router_fallback")
    return difference
