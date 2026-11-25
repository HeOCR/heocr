"""Offline specialist dispatch from caller categories and capture evidence."""

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path
import re
import struct

from heocr.models import MODELS
from heocr.ocr.routes import CAPTURE_MODELS, PHOTO_CAPTURES
from heocr.ocr.tesseract import OcrUnavailable

CATEGORIES = ("modern-unpointed", "pointed", "old-rabbinic", "phone")
CAPTURES = (*CAPTURE_MODELS, "camera-photo", "pdf-render")
# Rivok Full float from revision 9253cacf097ab396a2fc36d633737c43af5c28a3.
# The shipped int8 counterpart has the same two components, byte for byte.
RIVOK_FLOAT_SHA256 = "673a4193be1f77565942a69480f3c28c7de9633f25f1fa318162f18fa258ba1b"
RIVOK_CODEC = (
    "7c0767d6ffa78e731495c8e0d6bb8c2d2bf074fdff2ce3288f22e7c92a10dbf6",
    "1ecf120d457b96a1fbceed5df7343a642a990bff5d8af1eda7a4963861c2a60f",
)


def codec_hashes(path):
    """Read the Tesseract LSTM unicharset and recoder entries (slots 21 and 22)."""
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(0)
        prefix = stream.read(4)
        count = int.from_bytes(prefix, "little")
        if not 23 <= count <= 64:
            raise ValueError("invalid traineddata directory")
        directory = stream.read(8 * count)
        if len(directory) != 8 * count:
            raise ValueError("truncated traineddata directory")
        offsets = struct.unpack(f"<{count}q", directory)
        present = tuple(offset for offset in offsets if offset != -1)
        if (not present or present[0] < 4 + len(directory) or present[-1] >= size
                or any(a >= b for a, b in zip(present, present[1:])) or offsets[21] < 0 or offsets[22] < 0):
            raise ValueError("invalid traineddata component offsets")
        end = next((offset for offset in offsets[23:] if offset >= 0), size)
        hashes = []
        for start, stop in ((offsets[21], offsets[22]), (offsets[22], end)):
            if stop - start > 1024 * 1024:
                raise ValueError("oversized traineddata codec component")
            stream.seek(start)
            hashes.append(sha256(stream.read(stop - start)).hexdigest())
        return tuple(hashes)


@dataclass(frozen=True)
class Model:
    name: str
    path: Path | None = None
    sha256: str | None = None

    def engine_options(self):
        if self.path is None:
            return {"model": self.name}
        return {"local_pin": (self.path, self.name, self.sha256)}

    def check_codec(self, engine):
        if self.path is None:
            return
        try:
            compatible = codec_hashes(engine.tessdata / f"{self.name}.traineddata") == RIVOK_CODEC
        except (OSError, ValueError):
            compatible = False
        if not compatible:
            raise OcrUnavailable("specialist codec does not match the pinned Rivok float parent")
        return RIVOK_FLOAT_SHA256


def _model(value, directory):
    if isinstance(value, str) and value in MODELS:
        return Model(value)
    if isinstance(value, dict) and set(value) == {"path", "sha256"}:
        path, digest = value["path"], value["sha256"]
        if (isinstance(path, str) and "\x00" not in path and Path(path).suffix == ".traineddata" and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            return Model("heocr_specialist", directory / path, digest.lower())
    raise ValueError("specialist model must be a registered name or a .traineddata path with SHA-256")


def load_config(path):
    source = Path(path) if path is not None else files("heocr").joinpath("models_data", "specialists.json")
    try:
        raw = source.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read specialist routing config") from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "routes"}
            or data["schema"] != "heocr.specialists.v1" or not isinstance(data["routes"], list)):
        raise ValueError("specialist routing config must use heocr.specialists.v1")
    rules = {}
    directory = Path(path).absolute().parent if path is not None else Path.cwd()
    for row in data["routes"]:
        if (not isinstance(row, dict) or not {"category", "model"} <= row.keys()
                or row.keys() - {"category", "capture", "model"}
                or row["category"] not in CATEGORIES or ("capture" in row and row["capture"] not in CAPTURES)):
            raise ValueError("invalid specialist routing rule")
        key = (row["category"], row.get("capture"))
        if key in rules:
            raise ValueError("duplicate specialist routing rule")
        rules[key] = _model(row["model"], directory)
    return rules, sha256(raw).hexdigest()


def select(rules, category, capture):
    """Explicit categories take precedence. An inferred phone uses its own category."""
    inferred = category is None and capture in PHOTO_CAPTURES
    if inferred:
        category = "phone"
    model = rules.get((category, capture)) or rules.get((category, None))
    reason = ("phone_capture" if inferred else "declared_category") if model is not None else "default"
    return model, {"mode": "specialist", "category": category, "capture_type": capture, "reason": reason}
