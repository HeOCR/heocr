"""Fill heocr/models_data/ with the bundled models (D6), before a wheel is built.

    python scripts/fetch_models.py                      # from each model's release URL
    python scripts/fetch_models.py --from DIR           # from local files named <name>.traineddata in DIR

Every file is checked against its pin in heocr.models.MODELS (sha256 and size) before it is kept; nothing
unpinned is ever copied. The models are not committed to the repository: the build fetches them.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heocr.models import MODELS  # noqa: E402

TARGET = ROOT / "src" / "heocr" / "models_data"


def _check(path: Path, model) -> None:
    body = path.read_bytes()
    if len(body) != model.size or hashlib.sha256(body).hexdigest() != model.sha256:
        raise SystemExit(f"fetch_models: {path.name} does not match the pin of {model.name}; nothing kept")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="source", type=Path, help="a folder holding <name>.traineddata files")
    args = parser.parse_args(argv)
    bundled = [m for m in MODELS.values() if m.bundled]
    TARGET.mkdir(exist_ok=True)
    for model in bundled:
        target = TARGET / f"{model.name}.traineddata"
        part = target.with_suffix(".part")
        if args.source:
            shutil.copyfile(args.source / f"{model.name}.traineddata", part)
        else:
            with urllib.request.urlopen(model.url, timeout=120) as response, part.open("wb") as out:  # noqa: S310 (pinned https URL)
                shutil.copyfileobj(response, out)
        _check(part, model)
        part.replace(target)
        print(f"fetch_models: {model.name} ({model.size} bytes, sha256 {model.sha256[:12]}) ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
