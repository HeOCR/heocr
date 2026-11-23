"""CPU recognition for already segmented Hebrew print lines."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

SCHEMA = "heocr.crnn.line.v1"
HEIGHT = 32
STRIDE = 4
MAX_WIDTH = 2048


def line_box(image, bbox, scale: float) -> tuple[int, int, int, int]:
    """Convert a working-raster box from points to clipped pixel bounds."""
    x0, y0, x1, y1 = bbox
    return (
        max(0, math.floor(x0 * scale)),
        max(0, math.floor(y0 * scale)),
        min(image.width, math.ceil(x1 * scale)),
        min(image.height, math.ceil(y1 * scale)),
    )


def input_width(width: int, height: int) -> int:
    proportional = (width * HEIGHT + height - 1) // height
    return min(MAX_WIDTH, max(STRIDE, ((proportional + STRIDE - 1) // STRIDE) * STRIDE))


def ctc_characters(tokens, alphabet: tuple[str, ...]) -> str:
    """Collapse adjacent repeats before removing CTC's blank (class zero)."""
    letters = []
    previous = 0
    for token in tokens:
        if token and token != previous:
            letters.append(alphabet[token - 1])
        previous = token
    return "".join(letters)


class CrnnReader:
    """A local SHA-256-pinned CRNN model with a dynamic-width, batch-one interface."""

    def __init__(self, model, sha256: str):
        if not isinstance(sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
            raise ValueError("CRNN needs a 64-character model SHA-256")
        data = Path(model).read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        if self.sha256 != sha256.lower():
            raise ValueError("CRNN model SHA-256 mismatch")
        try:
            import numpy as np
            import onnxruntime as ort
            from bidi.algorithm import get_display
        except ImportError as exc:
            raise ValueError("CRNN needs the optional heocr[crnn] dependencies") from exc

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        try:
            self._session = ort.InferenceSession(data, sess_options=options, providers=["CPUExecutionProvider"])
        except Exception as exc:
            # ORT exposes different exception classes for invalid model formats and graphs.
            raise ValueError("Cannot load the supplied CRNN ONNX model") from exc
        metadata = self._session.get_modelmeta().custom_metadata_map
        alphabet = json.loads(metadata.get("heocr.crnn.alphabet", "null"))
        if (metadata.get("heocr.crnn.schema") != SCHEMA or not isinstance(alphabet, list)
                or not alphabet or any(not isinstance(char, str) or len(char) != 1 for char in alphabet)
                or len(set(alphabet)) != len(alphabet)):
            raise ValueError("CRNN ONNX model has invalid line-reader metadata")
        inputs, outputs = self._session.get_inputs(), self._session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError("CRNN ONNX model needs one image input and one logits output")
        image, logits = inputs[0], outputs[0]
        if (image.type != "tensor(float)" or logits.type != "tensor(float)"
                or len(image.shape) != 4 or image.shape[:3] != [1, 1, HEIGHT]
                or isinstance(image.shape[3], int) or len(logits.shape) != 3
                or logits.shape[0] != 1 or isinstance(logits.shape[1], int)
                or logits.shape[2] != len(alphabet) + 1):
            raise ValueError("CRNN ONNX model has incompatible image or logits dimensions")
        self.alphabet = tuple(alphabet)
        self._input, self._output = image.name, logits.name
        self._np = np
        self._get_display = get_display
        self.version = f"onnxruntime {ort.__version__}"

    def recognize(self, crop) -> str:
        from PIL import Image

        width = input_width(*crop.size)
        image = crop.convert("L").resize((width, HEIGHT), Image.Resampling.BILINEAR)
        pixels = self._np.asarray(image, dtype=self._np.float32)
        tensor = (1.0 - pixels / 255.0)[None, None, :, :]
        try:
            logits, = self._session.run([self._output], {self._input: tensor})
        except Exception as exc:
            raise ValueError("CRNN ONNX line inference failed") from exc
        if logits.shape != (1, width // STRIDE, len(self.alphabet) + 1):
            raise ValueError("CRNN ONNX model returned incompatible line logits")
        text = ctc_characters(logits[0].argmax(axis=-1), self.alphabet)
        return self._get_display(text, base_dir="R")


def read_page_lines(doc, blocks, image, scale: float) -> int:
    """Read the existing prose boxes in order. Return the change in character count."""
    from heocr.contract import time_up
    from heocr.textflags import text_flags

    reader = doc._crnn
    difference = 0
    for block in blocks:
        if not block.lines or block.source != "ocr" or block.kind != "text":
            continue
        boxes = [line_box(image, line.bbox, scale) for line in block.lines]
        if time_up(doc) or any(x0 >= x1 or y0 >= y1 for x0, y0, x1, y1 in boxes):
            block.flags.append("crnn_line_read_skipped")
            continue
        original = block.text
        readings = [reader.recognize(image.crop(box)) for box in boxes]
        for line, reading in zip(block.lines, readings, strict=True):
            line.text = reading
        block.text = "\n".join(line.text for line in block.lines)
        difference += sum(not c.isspace() for c in block.text) - sum(not c.isspace() for c in original)
        stale = set(text_flags(original)) | {"numbers_from_second_reading", "latin_from_second_reading"}
        block.flags[:] = [flag for flag in block.flags if flag not in stale] + text_flags(block.text)
        if any(not reading.strip() for reading in readings):
            block.flags.append("crnn_empty_line")
        block.ocr = {"engine": "crnn", "layout_engine": "tesseract", "dpi": round(scale * 72, 1),
                     "model_sha256": reader.sha256, "status": "unverified"}
        block.tool = reader.version
        block.checks = None
    return difference
