"""Measure the four-way OCR router on a local, hash-pinned DEV manifest."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from heocr import extract
from heocr.models import MODELS, resolve
from heocr.ocr.routes import DOMAINS, candidates


def measure(manifest: Path, *, tesseract: str | None = None) -> dict:
    raw = manifest.read_bytes()
    data = json.loads(raw)
    if data.get("split") != "dev" or not isinstance(data.get("rows"), list) or not data["rows"]:
        raise ValueError("expected a nonempty DEV routing manifest")
    root = manifest.resolve().parent
    seen = set()
    inputs = []
    for row in data["rows"]:
        ident, expected = row["id"], row["domain"]
        if not isinstance(ident, str) or not ident or ident in seen or expected not in DOMAINS:
            raise ValueError("DEV rows need unique IDs and known domains")
        relative = Path(row["path"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root):
            raise ValueError(f"{ident}: input path must stay below the manifest directory")
        if sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"{ident}: input digest differs")
        seen.add(ident)
        inputs.append((row, path))

    pins = {}
    for domain, model in candidates().items():
        resolve(model)
        released = MODELS[model]
        pins[domain] = {"name": model, "sha256": released.sha256, "hf_revision": released.hf_revision}
    rows = []
    matrix = {expected: dict.fromkeys((*DOMAINS, "unavailable"), 0) for expected in DOMAINS}
    for row, path in inputs:
        body = path.read_bytes()
        if sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"{row['id']}: input changed after preflight")
        document = extract(body, ocr_model="auto", tesseract=tesseract, check_numbers=False, convert=False)
        route = (document.options.get("ocr") or {}).get("model_route", {})
        expected = row["domain"]
        observed = route.get("domain")
        engine_failures = {"routed_model_unavailable", "ocr_call_failed"} & set(document.flags)
        page_errors = {"ocr_route_unavailable", "ocr_failed"} & {item.reason for item in document.unread}
        if (not document.blocks or observed not in DOMAINS or route.get("unavailable") or route.get("probe_unavailable")
                or engine_failures or page_errors):
            observed = "unavailable"
        matrix[expected][observed] += 1
        rows.append({"id": row["id"], "input_sha256": row["sha256"], "expected": expected,
                     "observed": observed, "decision": route,
                     "ocr_routes": [block.ocr["model_route"] for block in document.blocks
                                    if block.ocr and "model_route" in block.ocr]})
    correct = sum(matrix[domain][domain] for domain in DOMAINS)
    unavailable = sum(matrix[domain]["unavailable"] for domain in DOMAINS)
    return {"split": "dev", "manifest_sha256": sha256(raw).hexdigest(),
            "config": {"ocr_model": "auto", "check_numbers": False, "convert": False},
            "model_pins": pins, "documents": len(rows), "correct": correct, "unavailable": unavailable,
            "accuracy": correct / len(rows), "confusion_matrix": matrix, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--tesseract", help="path to a pinned Tesseract binary")
    parser.add_argument("--output", type=Path, help="write private JSON here instead of stdout")
    args = parser.parse_args()
    result = measure(args.manifest, tesseract=args.tesseract)
    report = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return int(bool(result["unavailable"]))


if __name__ == "__main__":
    raise SystemExit(main())
