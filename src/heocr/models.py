"""HeOCR's OCR models: a pinned registry, an explicit download, and resolution.

Each model is an openly released Tesseract ``.traineddata`` pinned by SHA-256.
The registry includes HeOCR's releases and optional upstream components.

- **No implicit download.** The core never downloads anything on its own. A model is a file
  the user supplies by path, or one downloaded on purpose with ``heocr --download-model NAME``
  (``download()``). The download is checked against the pinned sha256 and size before it is
  kept.
- **Bundled models.** ``heocr_modern_v1`` (the default OCR model since 0.2.0) and
  ``heocr_oldprint_v1`` can ship in model-enabled wheels and resolve without a download.
  Plain source builds use the same pins and resolve named models from the local cache.
  ``ocr_model="heb"`` (or ``"stock"``) reads with stock Tesseract ``heb``.
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
    url: str  # the published weight file
    doi: str | None  # the release DOI, when one exists
    licence: str
    domain: str = "unassigned"  # the print it is meant for (heocr.ocr.routes); set from evaluation's default-model study
    bundled: bool = False  # shipped inside the wheel (heocr/models_data/), so it needs no download


MODELS: dict[str, ReleasedModel] = {
    model.name: model
    for model in (
        ReleasedModel(
            name="heocr_r8b",
            sha256="8cb22dbeb195c3c0ffbe9c66cf956d8c2052d3b1c4a872234a85c975c4526117",
            size=3_633_477,
            url="https://huggingface.co/HeOCR/heocr-r8b-tesseract-heb/resolve/main/heocr_r8b.traineddata",
            doi="10.5281/zenodo.24372346",
            licence="Apache-2.0",
        ),
        ReleasedModel(
            # Office-print A/B v2 arm A (snapshot 0160m_222700): evaluation's default-model study (2026-11-19).
            name="heocr_modern_v1",
            sha256="6275943bf68f3d44ecc32f8d4c4195c890c96602111542ebe1f0ae0d8a6a5201",
            size=3_704_077,
            url="https://huggingface.co/HeOCR/heocr-modern-v1-tesseract-heb/resolve/main/heocr_modern_v1.traineddata",
            doi="10.5281/zenodo.24395097",
            licence="Apache-2.0",
            domain="modern",
            bundled=True,
        ),
        ReleasedModel(
            # The P-hist v1 pick (r7 arm, heb_final), continued from R7: old and rabbinic print.
            name="heocr_oldprint_v1",
            sha256="f8090760b5b02d09cf4ddb435e7a5e34089f6f17cde9cfb6c67fd06c6f17ec02",
            size=3_628_595,
            url="https://huggingface.co/HeOCR/heocr-oldprint-v1-tesseract-heb/resolve/main/heocr_oldprint_v1.traineddata",
            doi="10.5281/zenodo.24395013",
            licence="Apache-2.0",
            domain="old",
            bundled=True,
        ),
        ReleasedModel(
            name="heocr_r7",
            sha256="95faab81fba6fe57f854f973c1ccad2b0d1059fb951f34b17277385ceea8b303",
            size=3_628_595,
            url="https://huggingface.co/HeOCR/heocr-r7-tesseract-heb/resolve/main/heocr_r7.traineddata",
            doi="10.5281/zenodo.24372343",
            licence="Apache-2.0",
        ),
        ReleasedModel(
            name="heb_rivok",
            sha256="09ef1830f3393c590791abf9c18deca14450c8e5d3b556249d63b0183cf7a426",
            size=5_678_607,
            url=("https://huggingface.co/Rivok/tesseract-heb-rivok/resolve/"
                 "9253cacf097ab396a2fc36d633737c43af5c28a3/tessdata/heb_rivok.traineddata"),
            doi=None,
            licence="Apache-2.0",
        ),
        ReleasedModel(
            name="heb_rivok_lite",
            sha256="59da073ad0bca9449fa5a965996e49d0e2bee3c17e8b0b4e2769d67a7ce34c40",
            size=3_486_882,
            url=("https://huggingface.co/Rivok/tesseract-heb-rivok/resolve/"
                 "9253cacf097ab396a2fc36d633737c43af5c28a3/tessdata/heb_rivok_lite.traineddata"),
            doi=None,
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


def bundled_dir() -> Path:
    """Where models shipped inside the package live (``heocr/models_data/``); it may be empty."""
    from importlib.resources import files

    return Path(str(files("heocr") / "models_data"))


def resolve(spec: str) -> tuple[Path, str, ReleasedModel | None]:
    """(file, language name, released model or None) for a model name or a ``.traineddata`` path.

    A name resolves to the download cache first (``cache_dir``), then to the copy bundled in the
    package, if any (``bundled_dir``). A released model's file must match its pinned sha256,
    wherever it came from.
    """
    path = Path(spec)
    if path.suffix == ".traineddata" or path.is_file():
        if not path.is_file():
            raise ModelError(f"no model file {path.name}")
        name = path.name[: -len(".traineddata")] if path.name.endswith(".traineddata") else path.stem
    elif spec in MODELS:
        name = spec
        path = cache_dir() / f"{spec}.traineddata"
        if not path.is_file() and (bundled_dir() / f"{spec}.traineddata").is_file():
            path = bundled_dir() / f"{spec}.traineddata"
        if not path.is_file():
            raise ModelError(f"model {spec} is not downloaded; run: heocr --download-model {spec}")
    else:
        raise ModelError(f"unknown model {spec!r}; known: {', '.join(sorted(MODELS))}, or a path to a .traineddata file")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ModelError(f"model name {name!r} must use letters, digits, underscores or hyphens")
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
