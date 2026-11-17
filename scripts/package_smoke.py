"""D5a: the installed package, not the source tree, reads every kind of input from its CLI.

Run from the repository root with the installed ``heocr`` (a wheel in a fresh venv):

    python scripts/package_smoke.py

The fixtures are the synthetic ones of the test suite (invented text only). The script checks:

- **the installed package is the one under test,** not ``src/``;
- **its declared dependencies** are exactly the core three;
- **its licence files** (LICENSE and NOTICE) ship with it;
- **the ``heocr`` entry point** reads a synthetic DOCX, PDF, HTML, XLSX and PPTX, and JSON output is a valid ``heocr_document.v1`` record;
- **every runtime dependency's licence** is on the allow-list.

No OCR and no LibreOffice are needed. Exit code 0 means every check passed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # for tests.synth only; heocr itself must come from the installed package

CORE = {"defusedxml", "pypdfium2", "pillow"}
# Licences a runtime dependency may have (SPDX ids, or the name a package's metadata uses).
# Copyleft is never in the default install (decision 0020).
ALLOWED = re.compile(r"\b(MIT|MIT-CMU|BSD|BSD-2-Clause|BSD-3-Clause|Apache-2\.0|Apache Software License|PSF|PSFL|Python Software Foundation|HPND|ISC|Zlib)\b", re.I)


def fail(message: str) -> None:
    print(f"package smoke: FAIL: {message}")
    raise SystemExit(1)


def licence_of(dist) -> str:
    meta = dist.metadata
    return " ".join(
        [meta.get("License-Expression") or "", meta.get("License") or ""] + [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    )


def main() -> None:
    import heocr

    location = Path(heocr.__file__).resolve()
    if ROOT / "src" in location.parents:
        fail(f"heocr was imported from the source tree ({location}), not from the installed package")
    dist = metadata.distribution("heocr")
    requires = {re.split(r"[ ;<>=!~\[]", r, maxsplit=1)[0].lower() for r in dist.requires or [] if "extra ==" not in r}
    if requires != CORE:
        fail(f"declared dependencies {sorted(requires)} are not the core {sorted(CORE)}")
    files = {Path(str(f)).name for f in dist.files or []}
    for name in ("LICENSE", "NOTICE"):
        if name not in files:
            fail(f"{name} does not ship with the package")
    for name in sorted(CORE):
        text = licence_of(metadata.distribution(name))
        if not ALLOWED.search(text) or re.search(r"\bGPL\b|General Public", text):
            fail(f"{name}'s licence is not on the allow-list: {text!r}")

    from tests.synth import html as H
    from tests.synth.docx import Docx, para, table
    from tests.synth.pdf import PdfPage, lines_block, make_pdf
    from tests.synth.pptx import Deck, Slide, shape
    from tests.synth.pptx import para as slide_para
    from tests.synth.xlsx import Sheet, Workbook, c, row

    fixtures = {
        "a.docx": (Docx(body=[para("שלום עולם"), table([["שם", "סכום"], ["עט", "5"]])]).build(), ["שלום עולם", "עט | 5"]),
        "b.pdf": (make_pdf([PdfPage(lines=lines_block(["המציע יגיש את הצעתו", "עד המועד הקבוע"]))]), ["המציע יגיש את הצעתו"]),
        "c.html": (H.nevo_like_flow(), ["חובת פרסום"]),
        "d.xlsx": (Workbook(sheets=[Sheet("גיליון", rows=[row(1, [c("A1", inline="שם"), c("B1", 5)])])]).build(), ["שם | 5"]),
        "e.pptx": (Deck(slides=[Slide([shape([slide_para("שקף ראשון")], (50, 50, 300, 40))])]).build(), ["שקף ראשון"]),
    }
    exe = shutil.which("heocr")
    if exe is None:
        fail("the heocr entry point is not on the PATH")
    with tempfile.TemporaryDirectory() as tmp:
        for name, (body, expected) in fixtures.items():
            path = Path(tmp) / name
            path.write_bytes(body)
            text = subprocess.run([exe, str(path), "--no-ocr", "-q"], capture_output=True, check=True).stdout.decode("utf-8")
            for piece in expected:
                if piece not in text:
                    fail(f"{name}: {piece!r} not in the CLI's text output")
            record = json.loads(subprocess.run([exe, str(path), "--no-ocr", "-q", "-f", "json"], capture_output=True, check=True).stdout.decode("utf-8"))
            errors = heocr.validate(record)
            if errors:
                fail(f"{name}: the JSON record is not valid: {errors[:3]}")
        schema = heocr.contract.json_schema()  # shipped as package data (D5b)
        if schema.get("title") != heocr.contract.SCHEMA_ID:
            fail(f"the shipped JSON Schema is {schema.get('title')!r}, not {heocr.contract.SCHEMA_ID!r}")
        version = subprocess.run([exe, "--version"], capture_output=True, check=True, text=True).stdout.strip()
        if version != f"heocr {dist.version}":
            fail(f"--version says {version!r}, the package is {dist.version}")
    print(f"package smoke: OK (heocr {dist.version} from {location.parent}; {len(fixtures)} formats; dependencies {sorted(requires)})")


if __name__ == "__main__":
    main()
