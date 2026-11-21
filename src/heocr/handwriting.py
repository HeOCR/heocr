"""Optional Kraken 7.2 OCR for explicitly identified handwritten scan pages."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from importlib.metadata import version
from importlib.resources import as_file, files
from pathlib import Path
from statistics import mean
from typing import Literal

from heocr.contract import Block, Document, Line, Page, Unread, time_up
from heocr.ocr.regions import has_ink, short_for_ink
from heocr.ocr.tesseract import OcrLine, OcrResult

KRAKEN_VERSION = "7.2"
BUNDLED_SEGMENTER_SHA256 = (
    "a3ccc7309fe8df62d1cbd372706edc73db6a2a32806e3758522ed8528a734073"
)
RTL = "horizontal-rl"
HANDWRITING_TAGS = frozenset(("handwriting", "handwritten"))
SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class HandwritingConfig:
    """Local pinned recognizer and an optional handwriting-tagging segmenter."""

    model: str | Path
    sha256: str
    mode: Literal["auto", "force"] = "auto"
    segmenter: str | Path | None = None
    segmenter_sha256: str | None = None

    def __post_init__(self):
        if self.mode not in ("auto", "force"):
            raise ValueError("handwriting mode must be auto or force")
        if (
            not SHA256.fullmatch(self.sha256)
            or Path(self.model).suffix != ".safetensors"
        ):
            raise ValueError(
                "handwriting requires a local .safetensors model and a lowercase SHA-256"
            )
        if (self.segmenter is None) != (self.segmenter_sha256 is None):
            raise ValueError(
                "handwriting segmenter and its SHA-256 must be provided together"
            )
        if self.segmenter is not None and (
            Path(self.segmenter).suffix != ".safetensors"
            or not SHA256.fullmatch(self.segmenter_sha256)
        ):
            raise ValueError(
                "handwriting requires a local .safetensors segmenter and a lowercase SHA-256"
            )
        if self.mode == "auto" and self.segmenter is None:
            raise ValueError(
                "automatic handwriting requires a handwriting-tagging segmenter"
            )

    def provenance(self):
        return {
            "mode": self.mode,
            "recognizer": f"sha256:{self.sha256}",
            "segmenter": f"sha256:{self.segmenter_sha256 or BUNDLED_SEGMENTER_SHA256}",
        }


def _check_pin(path, expected):
    with Path(path).open("rb") as data:
        if hashlib.file_digest(data, "sha256").hexdigest() != expected:
            raise ValueError("Kraken checkpoint checksum mismatch")


@dataclass
class _Kraken:
    segmenter: object
    recognizer: object
    segment_config: object
    recognition_config: object

    def segment(self, image):
        return self.segmenter.predict(image, self.segment_config)

    def recognize(self, image, segmentation):
        return self.recognizer.predict(image, segmentation, self.recognition_config)


def _load(config):
    if version("kraken") != KRAKEN_VERSION:
        raise ValueError("unsupported Kraken version")
    _check_pin(config.model, config.sha256)
    from kraken.configs import RecognitionInferenceConfig, SegmentationInferenceConfig
    from kraken.tasks import RecognitionTaskModel, SegmentationTaskModel

    if config.segmenter is None:
        with as_file(files("kraken").joinpath("blla.mlmodel")) as bundled:
            _check_pin(bundled, BUNDLED_SEGMENTER_SHA256)
            segmenter = SegmentationTaskModel.load_model(bundled)
    else:
        _check_pin(config.segmenter, config.segmenter_sha256)
        segmenter = SegmentationTaskModel.load_model(config.segmenter)
    return _Kraken(
        segmenter,
        RecognitionTaskModel.load_model(config.model),
        SegmentationInferenceConfig(
            accelerator="cpu", text_direction=RTL, raise_on_error=True
        ),
        RecognitionInferenceConfig(
            accelerator="cpu",
            num_line_workers=0,
            raise_on_error=True,
            bidi_reordering="R",
            text_direction=RTL,
        ),
    )


def _tagged(line):
    types = {tag.get("type") for tag in (line.tags or {}).get("type", ())}
    return bool(types) and types <= HANDWRITING_TAGS


def _box(record, size):
    if getattr(record, "bbox", None) is not None:
        x0, y0, x1, y1 = record.bbox
    else:
        xs, ys = zip(*record.boundary)
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        raise ValueError("invalid Kraken line geometry")
    w, h = size
    bbox = [max(0, x0), max(0, y0), min(w, x1), min(h, y1)]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("empty Kraken line geometry")
    return bbox


@dataclass
class HandwritingReader:
    """Keep an initialized backend, or its load failure, for one document."""

    config: HandwritingConfig
    engine: _Kraken | None = None
    load_failed: bool = False

    def read(self, doc, image):
        if self.engine is None and not self.load_failed:
            try:
                self.engine = _load(self.config)
            except Exception:
                # Backend import, version and weight errors must preserve the ordinary OCR fallback.
                self.load_failed = True
        if self.engine is None:
            return "handwriting_backend_unavailable", []
        if time_up(doc):
            return "handwriting_time_limit", []
        try:
            segmentation = self.engine.segment(image)
            lines = segmentation.lines
            if not lines:
                return "handwriting_no_lines", []
            if self.config.mode == "auto" and not all(_tagged(line) for line in lines):
                return "handwriting_not_detected", []
            if time_up(doc):
                return "handwriting_time_limit", []
            expected = [line.id for line in lines]
            if len(set(expected)) != len(expected):
                return "handwriting_incomplete", []
            reads = {}
            for record in self.engine.recognize(image, segmentation):
                text = record.prediction
                if (
                    record.id in reads
                    or not text.strip()
                    or "\n" in text
                    or "\r" in text
                ):
                    return "handwriting_incomplete", []
                conf = [
                    float(value) * 100
                    for value in record.confidences
                    if math.isfinite(value) and 0 <= value <= 1
                ]
                reads[record.id] = OcrLine(text, _box(record, image.size), conf)
            if set(reads) != set(expected):
                return "handwriting_incomplete", []
            return "handwriting_kraken", [reads[key] for key in expected]
        except Exception:
            # Do not include backend messages, which can contain paths or document text.
            return "handwriting_backend_failed", []


def read_page(doc: Document, image, page: Page, scale: float) -> bool:
    """Populate an OCR page on success and annotate the ordinary fallback otherwise."""
    gray = image.convert("L")
    if not has_ink(gray):
        page.flags.append("handwriting_no_ink")
        return False
    status, readings = doc._handwriting.read(doc, image)
    page.flags.append(status)
    if not readings:
        if time_up(doc):
            page.route = "none"
            page.coverage.update(chars_read=0, status="unread")
            doc.pages.append(page)
            doc.flags.append("time_limit_reached")
            doc.unread.append(
                Unread(scope="page", reason="time_limit", page=page.index)
            )
            return True
        return False
    short, outside = short_for_ink(gray, OcrResult(readings))
    doc.components["kraken"] = f"kraken {KRAKEN_VERSION}"
    pins = doc._handwriting.config.provenance()
    doc.components["kraken:recognizer"] = pins["recognizer"]
    doc.components["kraken:segmenter"] = pins["segmenter"]
    dpi = round(scale * 72, 1)
    for read in readings:
        x0, y0, x1, y1 = (v / scale for v in read.bbox)
        bbox = [x0, y0, min(x1, page.width), min(y1, page.height)]
        ocr = {
            "status": "unverified",
            "engine": "kraken",
            "lang": "heb",
            "dpi": dpi,
            "words": len(read.text.split()),
        }
        if read.confidences:
            ocr["mean_confidence"] = round(mean(read.confidences), 2)
        doc.blocks.append(
            Block(
                kind="text",
                source="ocr",
                tool=f"kraken {KRAKEN_VERSION}",
                page=page.index,
                text=read.text,
                bbox=bbox,
                direction="rtl",
                lines=[Line(read.text, bbox)],
                ocr=ocr,
                flags=["ocr_unverified", "handwriting_kraken"],
            )
        )
    if short:
        page.flags.append("ocr_short_for_ink")
        doc.unread.append(
            Unread(
                scope="page",
                reason="ocr_short_for_ink",
                page=page.index,
                detail=f"{outside:.0%} of the ink lies outside every OCR line",
            )
        )
    page.signals.update(render_dpi=dpi, ocr_engine="kraken")
    page.coverage.update(
        chars_read=0,
        chars_ocr=sum(len(line.text) for line in readings),
        ink_outside_ocr_share=outside,
        status="partial" if short else "ocr",
    )
    doc.pages.append(page)
    return True
