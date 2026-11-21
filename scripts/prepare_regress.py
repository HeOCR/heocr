"""Freeze a release QA bundle from a locally verified hdocsyngen pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import pypdfium2

SCHEMA = "heocr_regress.v1"
LIMITS = (
    ("p10", 72),
    ("mixed_direction", 164),
    ("p9_grain", 28),
    ("p8", 12),
    ("scanned_labels", 24),
)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pages(path):
    with pypdfium2.PdfDocument(path) as pdf:
        return tuple(tuple(pdf.get_page_size(i)) for i in range(len(pdf)))


def loads(path):
    return json.loads(path.read_bytes())


def inside(root, relative):
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("pilot asset must be a file inside the pilot directory")
    return target


def pilot_pdf(root, producer_dir, item):
    # Resolve producer output names in their own bundle directory. The digest
    # remains the authority when a pilot has moved between checkouts.
    path = inside(root, producer_dir / Path(item["path"]).name)
    if sha(path) != item["sha256"]:
        raise ValueError("pilot PDF digest mismatch")
    return path


def producer_inputs(root, manifest):
    groups = {name: [] for name in manifest["spec"]["producers"]}
    for entry in manifest["docs"]:
        record_file = inside(root, entry["result"])
        result = loads(record_file)
        for name, record in result["producers"].items():
            check = record.get("pdf_check")
            if not check or check["problems"]:
                continue
            plain_info = record["plain"]["outputs"]["pdf"]
            plain = pilot_pdf(root, record_file.parent / name, plain_info)
            gold_file = inside(root, record_file.parent.relative_to(root) / f"{result['doc_id']}.{name}.gold.json")
            gold = loads(gold_file)
            if gold["producer"]["pdf_sha256"] != plain_info["sha256"]:
                raise ValueError("producer gold does not match the plain PDF")
            groups[name].append((record, plain, gold_file, gold, pages(plain)))

    # Interleave producers so the world of the largest producer does not
    # exhaust a chain's page budget before another producer contributes.
    return [items[i] for i in range(max(map(len, groups.values()))) for items in groups.values() if i < len(items)]


def freeze(root, output, manifest_pin, p8_records, p8_pin):
    manifest_file = root / "manifest.json"
    if sha(manifest_file) != manifest_pin:
        raise ValueError("pilot manifest digest mismatch")
    source = loads(manifest_file)
    if source["schema"] != "hdocsyngen_pilot.v0":
        raise ValueError("unsupported hdocsyngen pilot manifest")
    if sha(p8_records) != p8_pin:
        raise ValueError("P8 record digest mismatch")
    derived_p8 = {(rec["doc_id"], rec["chain"]): rec["gs"] for rec in loads(p8_records)}
    inputs = producer_inputs(root, source)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / "pdf").mkdir()
    (output / "gold").mkdir()
    cases = []

    def add(chain, variant, pdf, gold_file, gold, geometry):
        actual = pages(pdf)
        # P9 and P10 use whole pixels at 200 DPI. PDF page sizes can
        # therefore differ from the source by up to one raster pixel.
        tolerance = 72 / 200 + 0.01 if variant in {"img200", "grain", "ocr_heb", "ocr_eng"} else 0.1
        if len(actual) != len(geometry) or any(
            abs(a - b) > tolerance for pair, expected in zip(actual, geometry) for a, b in zip(pair, expected)
        ):
            raise ValueError("derived PDF changed page count or page size")
        name = f"{gold['doc_id']}.{gold['producer']['name']}.{variant}"
        pdf_out = output / "pdf" / f"{name}.pdf"
        if pdf_out != pdf:
            shutil.copyfile(pdf, pdf_out)
        gold_out = output / "gold" / gold_file.name
        if not gold_out.exists():
            shutil.copyfile(gold_file, gold_out)
        cases.append({
            "id": name, "chain": chain, "variant": variant, "pages": len(geometry),
            "pdf": pdf_out.relative_to(output).as_posix(), "pdf_sha256": sha(pdf_out),
            "gold": gold_out.relative_to(output).as_posix(), "gold_sha256": sha(gold_out),
            "producer_pdf_sha256": gold["producer"]["pdf_sha256"],
        })

    for chain, budget in LIMITS:
        before = len(cases)
        for record, plain, gold_file, gold, geometry in inputs:
            p8 = {}
            if chain == "p10":
                variants = ("ocr_heb", "ocr_eng")
            elif chain == "mixed_direction":
                if not any(block.get("values") and block.get("ltr_runs") for block in gold["blocks"]):
                    continue
                variants = ("plain",)
            elif chain == "p9_grain":
                variants = ("grain",)
            elif chain == "p8":
                variants = ("outlines", "postscript")
                maker = gold["producer"]["name"]
                p8 = {
                    variant: derived_p8.get((gold["doc_id"], f"{maker}+gs_{route}"))
                    for variant, route in zip(variants, ("nooutputfonts", "ps_roundtrip"))
                }
                if not all(p8.values()):
                    continue
            else:
                if not any(block.get("label_producer") for block in gold["blocks"]):
                    continue
                variants = ("img200",)
            if chain != "p8" and not all(variant in record for variant in variants):
                continue
            cost = len(geometry) * len(variants)
            if cost > budget:
                continue
            for variant in variants:
                if chain == "p8":
                    derivative = p8[variant]
                    if derivative["input_sha256"] != gold["producer"]["pdf_sha256"]:
                        raise ValueError("P8 derivative does not match the producer PDF")
                    pdf = pilot_pdf(root, plain.parent, derivative["outputs"]["pdf"])
                    add(chain, variant, pdf, gold_file, gold, geometry)
                else:
                    pdf = pilot_pdf(root, plain.parent, record[variant]["outputs"]["pdf"])
                    add(chain, variant, pdf, gold_file, gold, geometry)
            budget -= cost
        if len(cases) == before:
            raise ValueError(f"pilot has no eligible cases for {chain}")

    target = output / "manifest.json"
    manifest = {
        "schema": SCHEMA, "purpose": "synthetic_engineering_qa", "pilot_sha256": manifest_pin,
        "producer_versions": source["versions"], "p8_records_sha256": p8_pin,
        "cases": cases,
    }
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Release QA bundle manifest SHA-256 {sha(target)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pilot-sha256", required=True)
    parser.add_argument("--p8-records", type=Path, required=True)
    parser.add_argument("--p8-sha256", required=True)
    args = parser.parse_args()
    freeze(args.pilot.resolve(strict=True), args.output.resolve(), args.pilot_sha256,
           args.p8_records, args.p8_sha256)


if __name__ == "__main__":
    main()
