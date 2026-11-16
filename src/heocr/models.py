"""HeOCR's released OCR models: a pinned registry, an explicit download, and resolution.

Each model is a Tesseract ``.traineddata`` that HeOCR released openly (Apache-2.0, decision
0014), pinned by sha256 to the file on its Zenodo record and its Hugging Face mirror.

- **No implicit download.** The core never downloads anything on its own. A model is a file
  the user supplies by path, or one downloaded on purpose with ``heocr --download-model NAME``
  (``download()``). The download is checked against the pinned sha256 and size before it is
  kept.
- **The default is stock.** The OCR route keeps stock Tesseract ``heb`` by default. A HeOCR
  model becomes the default for a document set only when evaluation shows it beating stock
  on that set (decisions 0018 and 0020).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReleasedModel:
    name: str  # the Tesseract language name: the file name without .traineddata
    sha256: str
    size: int
    url: str  # the Hugging Face mirror's file
    doi: str  # the Zenodo version DOI
    licence: str


MODELS: dict[str, ReleasedModel] = {
    model.name: model
    for model in (
        ReleasedModel(
            name="heocr_r8b",
            sha256="8cb22dbeb195c3c0ffbe9c66cf956d8c2052d3b1c4a872234a85c975c4526117",
            size=3_633_477,
            url="https://huggingface.co/HeOCR/heocr-r8b-tesseract-heb/resolve/main/heocr_r8b.traineddata",
            doi="10.5281/zenodo.24156688",
            licence="Apache-2.0",
        ),
        ReleasedModel(
            name="heocr_r7",
            sha256="95faab81fba6fe57f854f973c1ccad2b0d1059fb951f34b17277385ceea8b303",
            size=3_628_595,
            url="https://huggingface.co/HeOCR/heocr-r7-tesseract-heb/resolve/main/heocr_r7.traineddata",
            doi="10.5281/zenodo.24126449",
            licence="Apache-2.0",
        ),
    )
}

_MAX_BYTES = 200_000_000


class ModelError(RuntimeError):
    """A model cannot be used: not found, not downloaded, or not the pinned file."""


def cache_dir() -> Path:
    """Where downloaded models are kept: ``$HEOCR_MODELS``, else the user's cache folder."""
    configured = os.environ.get("HEOCR_MODELS")
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "heocr" / "models"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "heocr" / "models"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve(spec: str) -> tuple[Path, str, ReleasedModel | None]:
    """(file, language name, released model or None) for a model name or a ``.traineddata`` path.

    A released model's file must match its pinned sha256, wherever it came from.
    """
    path = Path(spec)
    if path.suffix == ".traineddata" or path.is_file():
        if not path.is_file():
            raise ModelError(f"no model file {path.name}")
        name = path.name[: -len(".traineddata")] if path.name.endswith(".traineddata") else path.stem
    elif spec in MODELS:
        name = spec
        path = cache_dir() / f"{spec}.traineddata"
        if not path.is_file():
            raise ModelError(f"model {spec} is not downloaded; run: heocr --download-model {spec}")
    else:
        raise ModelError(f"unknown model {spec!r}; known: {', '.join(sorted(MODELS))}, or a path to a .traineddata file")
    if not name.replace("_", "").isalnum():
        raise ModelError(f"model name {name!r} must be letters, digits and underscores")
    released = MODELS.get(name)
    if released is not None and sha256_of(path) != released.sha256:
        raise ModelError(f"{name}.traineddata is not the released file (sha256 differs from {released.sha256[:12]}…)")
    return path, name, released


def download(name: str, *, timeout: float = 120.0) -> Path:
    """Download a released model on purpose, check it against its pin, and keep it in the cache."""
    model = MODELS.get(name)
    if model is None:
        raise ModelError(f"unknown model {name!r}; known: {', '.join(sorted(MODELS))}")
    target = cache_dir() / f"{name}.traineddata"
    if target.is_file() and sha256_of(target) == model.sha256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{name}-", suffix=".part", delete=False) as part:
        try:
            with urllib.request.urlopen(model.url, timeout=timeout) as response:  # noqa: S310 (pinned https URL)
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    size += len(chunk)
                    if size > _MAX_BYTES:
                        raise ModelError(f"{name}: the download is larger than expected")
                    digest.update(chunk)
                    part.write(chunk)
        except BaseException:
            part.close()
            Path(part.name).unlink(missing_ok=True)
            raise
    if digest.hexdigest() != model.sha256 or (model.size and size != model.size):
        Path(part.name).unlink(missing_ok=True)
        raise ModelError(f"{name}: the downloaded file does not match its pinned sha256; nothing was kept")
    Path(part.name).replace(target)
    return target
