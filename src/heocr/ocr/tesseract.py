"""Stock Tesseract as an optional external program: the OCR route's baseline engine.

Tesseract (Apache-2.0) is never bundled. It is found as ``tesseract`` on the PATH, through
``HEOCR_TESSERACT``, or as passed by the caller. Without it the core still runs, and every
page that needed OCR is recorded as ``route_unavailable``.

Every run's provenance is recorded the way heocr-engine records it: the version string, the
executable's sha256, the sha256 of each ``.traineddata`` used, the language list and the
page segmentation mode. No local path enters the record.

Defaults (D2 plan, the lead's review):

- **Language:** ``heb`` only. ``heb+eng`` and other lists are an explicit user choice,
  because each one adds a model.
- **Page segmentation mode:** ``--psm 3``, fully automatic page segmentation without OSD.
  That is Tesseract's default for whole pages. Regions use the same mode, so a region
  with several lines is segmented too.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LANG = "heb"
DEFAULT_PSM = 3
RENDER_DPI = 300
TIMEOUT = 180.0
_LANG = re.compile(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*")
_VERSION = re.compile(r"tesseract v?[A-Za-z0-9.+_~-]+")
_LIST_LANGS = re.compile(r'List of available languages in "(.+?)"')
_MAX_MODEL_BYTES = 500_000_000


class OcrUnavailable(RuntimeError):
    """The OCR route cannot run here; the reason is recorded, never raised to the caller."""


@dataclass
class OcrWord:
    text: str
    bbox: list[float]  # pixels of the recognized image, top-left origin
    confidence: float


@dataclass
class OcrLine:
    text: str
    bbox: list[float]  # pixels of the recognized image, top-left origin
    confidences: list[float] = field(default_factory=list)
    paragraph: tuple[str, str] = ("0", "0")  # Tesseract's (block, paragraph)
    words: list[OcrWord] = field(default_factory=list)  # in Tesseract's order; ``text`` is them joined
    numbers_replaced: int = 0  # words replaced by a second reading of numbers (pdf.reader)
    number_readings: list[dict] = field(default_factory=list)  # each replacement, both readings (pdf.reader)


@dataclass
class OcrResult:
    lines: list[OcrLine]

    @property
    def words(self) -> int:
        return sum(len(line.confidences) for line in self.lines)

    @property
    def mean_confidence(self) -> float | None:
        values = [c for line in self.lines for c in line.confidences]
        return round(sum(values) / len(values), 2) if values else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        read = 0
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            read += len(chunk)
            if read > _MAX_MODEL_BYTES:
                raise OcrUnavailable(f"{path.name} is larger than expected")
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise OcrUnavailable("tesseract not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise OcrUnavailable("tesseract timed out") from exc


def _private_tessdata(installation: Path, model_path: Path, name: str) -> Path:
    """A temporary tessdata folder: the model, plus the installation's ``configs`` for TSV output."""
    import atexit

    configs = installation / "configs"
    if not (configs / "tsv").is_file():
        raise OcrUnavailable("the Tesseract installation has no configs/tsv, needed for TSV output")
    folder = Path(tempfile.mkdtemp(prefix="heocr-tessdata-"))
    atexit.register(shutil.rmtree, folder, True)
    shutil.copyfile(model_path, folder / f"{name}.traineddata")
    shutil.copytree(configs, folder / "configs")
    return folder


def check_settings(lang: str, psm: int) -> None:
    """Reject a malformed language list or an unsupported page segmentation mode early."""
    if not isinstance(lang, str) or not _LANG.fullmatch(lang) or len(set(lang.split("+"))) != len(lang.split("+")):
        raise ValueError(f"invalid Tesseract language list {lang!r}")
    if not isinstance(psm, int) or not 3 <= psm <= 13 or psm == 12:
        raise ValueError(f"unsupported page segmentation mode {psm}; use 3-11 or 13")


@dataclass
class Tesseract:
    cmd: list[str]
    tessdata: Path
    lang: str
    psm: int
    version: str
    executable_sha256: str
    models: dict[str, str]  # language -> sha256 of its traineddata
    timeout: float = TIMEOUT
    released: object = None  # the heocr.models.ReleasedModel in use, if any
    route: dict | None = None  # ocr_model="auto": how this engine was chosen (heocr.ocr.routes)

    @classmethod
    def find(
        cls,
        cmd: str | Sequence[str] | None = None,
        *,
        lang: str = DEFAULT_LANG,
        psm: int = DEFAULT_PSM,
        tessdata: str | os.PathLike[str] | None = None,
        timeout: float = TIMEOUT,
        model: str | None = None,
    ) -> "Tesseract":
        """Find Tesseract, and the models for ``lang``.

        With ``model`` (a released HeOCR model's name, or a ``.traineddata`` path), that model
        replaces ``lang``. It runs from a private tessdata folder holding the model and a copy of
        the installation's ``configs`` folder, which Tesseract needs for TSV output once
        ``--tessdata-dir`` points elsewhere (the R8b card's note).
        """
        check_settings(lang, psm)
        if cmd is None:
            cmd = os.environ.get("HEOCR_TESSERACT") or shutil.which("tesseract")
        if not cmd:
            raise OcrUnavailable("tesseract not found")
        argv = [cmd] if isinstance(cmd, str) else list(cmd)
        version_out = _run([*argv, "--version"], timeout)
        lines = (version_out.stdout or version_out.stderr).decode("utf-8", "replace").splitlines()
        version = lines[0].strip() if lines else ""
        if version_out.returncode != 0 or not _VERSION.fullmatch(version):
            raise OcrUnavailable("tesseract --version gave no version")
        if tessdata is None:
            tessdata = os.environ.get("TESSDATA_PREFIX")
        if tessdata is None:
            listing = _run([*argv, "--list-langs"], timeout)
            match = _LIST_LANGS.search((listing.stdout + listing.stderr).decode("utf-8", "replace"))
            if not match:
                raise OcrUnavailable("tesseract did not report its tessdata folder")
            tessdata = match.group(1)
        folder = Path(tessdata)
        released = None
        if model is not None:
            from heocr.models import ModelError, resolve

            try:
                model_path, lang, released = resolve(model)
            except ModelError as exc:
                raise OcrUnavailable(str(exc)) from exc
            folder = _private_tessdata(folder, model_path, lang)
        models = {}
        for language in lang.split("+"):
            model = folder / f"{language}.traineddata"
            if not model.is_file():
                raise OcrUnavailable(f"no {language}.traineddata installed for tesseract")
            models[language] = _sha256(model)
        executable = shutil.which(argv[0]) or argv[0]
        executable_sha256 = _sha256(Path(executable)) if Path(executable).is_file() else "unknown"
        return cls(argv, folder, lang, psm, version, executable_sha256, models, timeout, released)

    def provenance(self) -> dict[str, str]:
        """Record components: versions and hashes only, no paths."""
        out = {
            "tesseract": self.version,
            "tesseract:executable_sha256": self.executable_sha256,
            "tesseract:lang": self.lang,
            "tesseract:psm": str(self.psm),
        }
        for language, sha in self.models.items():
            out[f"tesseract:{language}.traineddata"] = f"sha256:{sha}"
        if self.released is not None:
            out[f"tesseract:{self.released.name}.doi"] = self.released.doi
            out[f"tesseract:{self.released.name}.licence"] = self.released.licence
        return out

    def recognize(self, image, dpi: int = RENDER_DPI, psm: int | None = None) -> OcrResult:
        """Recognize a PIL image rendered at ``dpi``; lines in Tesseract's reading order, with word confidences.

        ``psm`` overrides the engine's page segmentation mode for this call (8 for a single word).
        """
        with tempfile.TemporaryDirectory(prefix="heocr-ocr-") as temp:
            source = Path(temp) / "input.png"
            image.save(source, dpi=(dpi, dpi))
            base = Path(temp) / "result"
            done = _run(
                [
                    *self.cmd,
                    str(source),
                    str(base),
                    "--tessdata-dir",
                    str(self.tessdata),
                    "-l",
                    self.lang,
                    "--psm",
                    str(self.psm if psm is None else psm),
                    "--dpi",
                    str(dpi),
                    "tsv",
                ],
                self.timeout,
            )
            tsv = base.with_suffix(".tsv")
            if done.returncode != 0 or not tsv.is_file():
                raise OcrUnavailable(f"tesseract failed with exit code {done.returncode}")
            return parse_tsv(tsv.read_text(encoding="utf-8", errors="replace"))


def parse_tsv(tsv: str) -> OcrResult:
    """Lines from Tesseract's TSV: words grouped by block, paragraph and line, in output order."""
    lines: dict[tuple[str, str, str, str], OcrLine] = {}
    order: list[tuple[str, str, str, str]] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        if not text:
            continue
        key = (cols[1], cols[2], cols[3], cols[4])
        left, top, width, height = (float(v) for v in cols[6:10])
        try:
            conf = float(cols[10])
        except ValueError:
            conf = -1.0
        box = [left, top, left + width, top + height]
        if key not in lines:
            lines[key] = OcrLine(text, box, [conf], (cols[2], cols[3]), [OcrWord(text, list(box), conf)])
            order.append(key)
        else:
            line = lines[key]
            line.text += " " + text
            line.bbox = [min(line.bbox[0], box[0]), min(line.bbox[1], box[1]), max(line.bbox[2], box[2]), max(line.bbox[3], box[3])]
            line.confidences.append(conf)
            line.words.append(OcrWord(text, list(box), conf))
    return OcrResult([lines[k] for k in order])
