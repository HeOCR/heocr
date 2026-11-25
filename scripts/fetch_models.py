"""Fill heocr/models_data/ with the bundled models (D6), before a wheel is built.

    python scripts/fetch_models.py                      # from each model's release URL
    python scripts/fetch_models.py --from DIR           # from local files named <name>.traineddata in DIR
    python scripts/fetch_models.py --check-wheel WHEEL  # verify the built artifact without downloading
    # Add --model heocr_modern_v1 to stage or verify just the default model.

Every file is checked against its pin in heocr.models.MODELS (sha256 and size) before it is kept; nothing
unpinned is ever copied. The models are not committed to the repository: the build fetches them.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
from zipfile import BadZipFile, ZipFile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heocr.models import MODELS  # noqa: E402

TARGET = ROOT / "src" / "heocr" / "models_data"


def _check(body: bytes, model) -> None:
    if len(body) != model.size or hashlib.sha256(body).hexdigest() != model.sha256:
        raise SystemExit(f"fetch_models: {model.name}.traineddata does not match the pinned hash and size")


def _check_wheel(path: Path, models) -> None:
    with ZipFile(path) as wheel:
        for model in models:
            member = f"heocr/models_data/{model.name}.traineddata"
            if wheel.namelist().count(member) != 1:
                raise SystemExit(f"fetch_models: expected exactly one {member} in the wheel")
            _check(wheel.read(member), model)
            print(f"fetch_models: wheel contains pinned {model.name} (sha256 {model.sha256})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--from", dest="source", type=Path, help="a folder holding <name>.traineddata files")
    mode.add_argument("--check-wheel", type=Path, help="verify selected model pins in a built wheel")
    bundled = {m.name: m for m in MODELS.values() if m.bundled}
    parser.add_argument("--model", choices=tuple(bundled), action="append", help="select models, default all bundled models")
    args = parser.parse_args(argv)
    models = [bundled[name] for name in (args.model or bundled)]
    if args.check_wheel is not None:
        try:
            _check_wheel(args.check_wheel, models)
        except (OSError, BadZipFile) as exc:
            parser.error(f"cannot read wheel: {exc}")
        return 0
    TARGET.mkdir(exist_ok=True)
    for model in models:
        target = TARGET / f"{model.name}.traineddata"
        part = target.with_suffix(".part")
        if args.source:
            shutil.copyfile(args.source / f"{model.name}.traineddata", part)
        else:
            with urllib.request.urlopen(model.url, timeout=120) as response, part.open("wb") as out:  # noqa: S310 (pinned https URL)
                shutil.copyfileobj(response, out)
        try:
            _check(part.read_bytes(), model)
            part.replace(target)
        finally:
            part.unlink(missing_ok=True)
        print(f"fetch_models: {model.name} ({model.size} bytes, sha256 {model.sha256[:12]}) ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
