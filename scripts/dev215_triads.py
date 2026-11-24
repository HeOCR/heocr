"""Register private DEV215 captures and score the frozen router v2 triads."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
from importlib import metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile

SLOTS = ("scanner", "phone-a", "phone-b")
ARMS = ("router_v2", "modern_v1", "rivok")
MODERN = "heocr_modern_v1"
RIVOK = "heb_rivok"
MODEL_PINS = {
    MODERN: "6275943bf68f3d44ecc32f8d4c4195c890c96602111542ebe1f0ae0d8a6a5201",
    RIVOK: "09ef1830f3393c590791abf9c18deca14450c8e5d3b556249d63b0183cf7a426",
    "line_v6": "52b94ef13eaa3c8d32403ab05e12fc9be74d22a2d1aaa4d2de904b313b0e1609",
}
THREADS = {"OMP_THREAD_LIMIT": "1", "VECLIB_MAXIMUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
HEADERS = {
    ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",), ".png": (b"\x89PNG\r\n\x1a\n",),
    ".tif": (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"),
}
FAILURES = {"ocr_failed", "ocr_call_failed", "ocr_route_unavailable", "time_limit"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "Expected a SHA-256 pin")
    return value


def read_pinned(path, expected):
    raw = path.read_bytes()
    require(digest(raw) == sha(expected), f"SHA-256 differs for {path.name}")
    return raw


def load(path, expected, schema):
    data = json.loads(read_pinned(path, expected))
    require(data.get("schema") == schema and data.get("split") == "dev", "Expected the pinned DEV215 schema")
    return data


def scoped_path(root, relative):
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts, "Expected a relative asset path")
    path = (root / path).resolve(strict=True)
    require(path.is_relative_to(root) and path.is_file(), "Asset leaves its input root")
    return path


def private_dir(path):
    require(path.resolve() == path.absolute(), "Use a physical private output directory")
    require(not any((ancestor / ".git").exists() for ancestor in (path, *path.parents)),
            "Keep capture and score output outside Git checkouts")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        require(path.stat().st_mode & 0o077 == 0, "Output directory must be private (0700)")


def publish(path, body):
    """Make a complete file visible once, and permit an exact replay."""
    if path.exists() or path.is_symlink():
        require(not path.is_symlink() and path.read_bytes() == body, f"Existing {path.name} differs")
        return
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
    except FileExistsError:
        require(not path.is_symlink() and path.read_bytes() == body, f"Existing {path.name} differs")
    finally:
        if temp is not None:
            temp.unlink()


def write_receipt(directory, label, data):
    raw = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    pin = digest(raw)
    publish(directory / f"{label}-{pin}.json", raw)
    return pin


def print_roster(path, pin):
    manifest = load(path, pin, "heocr.office.print.dev215.v1")
    sha(manifest["pdf_sha256"])
    sha(manifest["freeze_sha256"])
    roster = {}
    for page in manifest["pages"]:
        key = page["print_id"]
        require(re.fullmatch(r"DEV215-[0-9]{2}", key) and key not in roster, "Invalid or duplicate print ID")
        require(page["capture_slots"] == list(SLOTS), "Print packet slot list differs")
        sha(page["source_pdf_sha256"])
        sha(page["source_raster_sha256"])
        roster[key] = page
    require(len(roster) == 10 and len({p["dev_source_id"] for p in roster.values()}) == 10,
            "Expected the ten-page Thursday print roster")
    return manifest, roster


def device_map(selection):
    devices = selection["devices"]
    require(set(devices) == set(SLOTS), "Declare the three capture devices")
    require(all(re.fullmatch(r"[A-Za-z0-9_-]{1,40}", d["id"]) for d in devices.values()),
            "Use pseudonymous device IDs")
    require(len({d["id"] for d in devices.values()}) == len(SLOTS), "Device IDs must be distinct")
    require(devices["scanner"]["type"] == "scanner"
            and {devices[s]["type"] for s in SLOTS[1:]} == {"iphone", "pixel"},
            "Map the two phone slots to the confirmed iPhone and Pixel")
    return devices


def capture_format(body, suffix):
    if suffix == ".pdf":
        return b"%PDF-" in body[:1024]
    if suffix in (".heic", ".heif"):
        return body[4:8] == b"ftyp" and body[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
    return body.startswith(HEADERS.get(".tif" if suffix == ".tiff" else suffix, ()))


def register(args):
    manifest, roster = print_roster(args.print_manifest, args.print_sha)
    selection = load(args.selections, args.selection_sha, "heocr.office.selection.dev215.v1")
    require(selection["print_manifest_sha256"] == args.print_sha, "Selection names a different print packet")
    devices = device_map(selection)
    roots = {key: root.resolve(strict=True) for key, root in {
        "telegram": args.telegram, "photos": args.photos, "scanner": args.scanner,
    }.items() if root is not None}
    captures = {}
    originals = {}
    used_pages = set()
    instances = {}
    for row in selection["captures"]:
        print_id, slot = key = (row["print_id"], row["slot"])
        require(print_id in roster and slot in SLOTS and key not in captures, "Unknown or duplicate capture slot")
        provider = row["provider"]
        require(provider in roots, "Missing local export root for a selected capture")
        require(slot == "scanner" or provider != "scanner", "Phone capture must come from a photo or Telegram export")
        require(row["original"] is True, "Select the original capture file")
        instance = row["print_instance"]
        require(re.fullmatch(r"[A-Za-z0-9_-]{1,60}", instance)
                and instances.setdefault(print_id, instance) == instance, "Triad captures must use the same physical sheet")
        path = scoped_path(roots[provider], row["file"])
        suffix = path.suffix.lower()
        page_index, rotation = row["page_index"], row["rotation"]
        require(type(page_index) is int and page_index >= 0 and (suffix == ".pdf" or page_index == 0),
                "Use a zero-based PDF page index, or 0 for a raster")
        require(type(rotation) is int and rotation in (0, 90, 180, 270), "Invalid clockwise rotation")
        body = path.read_bytes()
        require(capture_format(body, suffix), "Capture extension and header differ or format is unsupported")
        pin = digest(body)
        capture_page = (pin, page_index)
        require(capture_page not in used_pages, "A raw capture page was assigned to multiple slots")
        used_pages.add(capture_page)
        original = f"{pin}{suffix}"
        originals.setdefault(original, path)
        captures[key] = {
            "print_id": print_id, "slot": slot, "print_instance": instance,
            "device": devices[slot], "provider": provider, "raw_file": f"raw/{original}",
            "raw_sha256": pin, "bytes": len(body), "page_index": page_index, "rotation": rotation,
            "source_id": roster[print_id]["dev_source_id"],
        }
    require(len(set(instances.values())) == len(instances), "Physical sheet IDs must be unique across print IDs")
    private_dir(args.out)
    raw_dir = args.out / "raw"
    private_dir(raw_dir)
    for filename, original in originals.items():
        body = read_pinned(original, Path(filename).stem)
        publish(raw_dir / filename, body)
    missing = [{"print_id": p, "slot": s} for p in sorted(roster) for s in SLOTS if (p, s) not in captures]
    receipt = {
        "schema": "heocr.office.registration.dev215.v1", "split": "dev",
        "print_manifest_sha256": args.print_sha, "selection_sha256": args.selection_sha,
        "source_freeze_sha256": manifest["freeze_sha256"], "print_pdf_sha256": manifest["pdf_sha256"],
        "captures": [captures[k] for k in sorted(captures)], "missing": missing,
    }
    pin = write_receipt(args.out, "registration", receipt)
    return {"registration_sha256": pin, "captures": len(captures), "missing": missing}


def checked_raster(raw, capture):
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        require(image.format == "PNG" and image.mode == "RGB" and image.size == tuple(capture["size"]),
                "Expected the frozen opaque RGB PNG size")
        require(all(abs(dpi - 144) < 0.1 for dpi in image.info.get("dpi", (0, 0))), "Expected a 144 DPI raster")
        require(not image.getexif() and not image.text and "xmp" not in image.info,
                "Prepared raster contains capture metadata")
        boxes = capture["witness_boxes"]
        require(type(capture["witness_outside_crop"]) is bool, "Freeze whether the crop excludes the witness")
        require(bool(boxes) != (capture["witness_outside_crop"] is True), "Freeze either witness masks or the approved crop")
        for box in boxes:
            left, top, right, bottom = box
            require(all(type(v) is int for v in box)
                    and 0 <= left < right <= image.width and 0 <= top < bottom <= image.height,
                    "Witness mask lies outside the frozen raster")
            require(image.crop(box).getextrema() == ((255, 255),) * 3, "Witness mask is not white")


def preflight(args):
    registration = load(args.registration, args.registration_sha, "heocr.office.registration.dev215.v1")
    freeze = load(args.freeze, args.freeze_sha, "heocr.office.score-freeze.dev215.v1")
    require(0 < len(freeze["pages"]) <= 10, "Freeze at least one complete registered triad")
    require(freeze["registration_sha256"] == args.registration_sha
            and freeze["source_freeze_sha256"] == registration["source_freeze_sha256"],
            "Score freeze and registration differ")
    sha(freeze["lineage_receipt_sha256"])
    sha(freeze["review_receipt_sha256"])
    registration_root = args.registration.parent.resolve(strict=True)
    asset_root = args.assets.resolve(strict=True)
    registered = {}
    for row in registration["captures"]:
        key = (row["print_id"], row["slot"])
        require(key not in registered, "Duplicate registered slot")
        path = scoped_path(registration_root, row["raw_file"])
        require(path.stat().st_size == row["bytes"] and hash_file(path) == sha(row["raw_sha256"]),
                "Original capture bytes differ")
        registered[key] = row
    seen = set()
    seen_rasters = set()
    jobs = []
    for page in freeze["pages"]:
        print_id = page["print_id"]
        require(isinstance(page["issue_cluster"], str) and page["issue_cluster"].strip(), "Missing issue cluster")
        require(set(page["captures"]) == set(SLOTS), "Freeze one of each capture slot per page")
        reference = json.loads(read_pinned(scoped_path(asset_root, page["reference"]), page["reference_sha256"]))
        require(reference["schema"] == "heocr.office.reference.dev215.v1" and reference["split"] == "dev"
                and reference["print_id"] == print_id and reference["source_id"] == page["source_id"],
                "Reference is outside the frozen leaf")
        require(isinstance(reference["lines"], list) and all(isinstance(line, str) for line in reference["lines"])
                and len(reference["lines"]) == len(reference["keep"]) and any(reference["keep"])
                and all(type(keep) is bool for keep in reference["keep"]), "Invalid common line mask")
        for slot in SLOTS:
            key = (print_id, slot)
            require(key in registered and key not in seen, "Frozen slot differs from the registration")
            seen.add(key)
            raw = registered[key]
            asset = page["captures"][slot]
            require(raw["raw_sha256"] == asset["raw_sha256"] and raw["source_id"] == page["source_id"]
                    and raw["page_index"] == asset["page_index"] and raw["rotation"] == asset["rotation"],
                    "Prepared raster names a different raw page or rotation")
            sha(asset["geometry_receipt_sha256"])
            path = scoped_path(asset_root, asset["file"])
            require(asset["sha256"] not in seen_rasters, "Prepared raster reused between slots")
            seen_rasters.add(asset["sha256"])
            checked_raster(read_pinned(path, asset["sha256"]), asset)
            jobs.append((page, slot, raw["device"]["type"], reference, path))
    complete = {key for key in registered if all((key[0], slot) in registered for slot in SLOTS)}
    require(seen == complete, "Freeze must cover the complete registered triads")
    return freeze, jobs


def runtime(args, freeze):
    checkout = args.checkout.resolve(strict=True)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    require(revision == freeze["heocr_commit"], "HeOCR checkout differs from the scoring freeze")
    require(not subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--", "src/heocr"]),
            "HeOCR source checkout must be clean")
    versions = freeze["dependencies"]
    require(set(versions) == {"Pillow", "defusedxml", "pypdfium2", "rapidfuzz"}, "Freeze the scoring dependencies")
    require(all(metadata.version(name) == version for name, version in versions.items()),
            "Scoring environment dependency version differs")
    sys.path.insert(0, str(checkout / "src"))
    import heocr

    require(Path(heocr.__file__).resolve().is_relative_to(checkout / "src"), "Imported HeOCR from the wrong checkout")
    models = args.models.resolve(strict=True)
    for name in (MODERN, RIVOK):
        require(hash_file(models / f"{name}.traineddata") == MODEL_PINS[name], f"Model pin differs for {name}")
    require(hash_file(args.line_v6) == MODEL_PINS["line_v6"], "Line v6 pin differs")
    require(hash_file(args.tesseract) == sha(freeze["tesseract_sha256"]), "Tesseract executable pin differs")
    tessdata = args.tessdata.resolve(strict=True)
    require(hash_file(tessdata / "eng.traineddata") == sha(freeze["eng_sha256"]), "Number-reader pin differs")
    require(hash_file(tessdata / "heb.traineddata") == sha(freeze["heb_sha256"]), "Latin verification reader pin differs")
    require(hash_file(args.scorer) == sha(freeze["scorer_sha256"]), "Page scorer pin differs")
    spec = importlib.util.spec_from_file_location("registered_page_cer", args.scorer)
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    os.environ.update(THREADS, HEOCR_MODELS=str(models), TESSDATA_PREFIX=str(tessdata))
    return heocr, scorer, revision


def score_one(heocr, body, carrier, args):
    options = {"tesseract": str(args.tesseract.resolve()), "check_numbers": "suspect",
               "force_ocr": True, "convert": False, "max_pages": 1}
    routes = {
        "router_v2": {"ocr_router": "v2", "ocr_capture_type": carrier,
                      "ocr_line_v6": args.line_v6 if carrier == "scanner" else None},
        "modern_v1": {"ocr_model": MODERN},
        "rivok": {"ocr_model": RIVOK},
    }
    docs = {}
    for arm, route in routes.items():
        doc = heocr.extract(body, **options, **route)
        require(len(doc.pages) == 1 and doc.pages[0].route == "ocr", f"Unexpected OCR route for {arm}")
        require(not any(item.reason in FAILURES for item in doc.unread)
                and not FAILURES.intersection(doc.flags)
                and "ocr_line_v6_unavailable" not in doc.flags, f"OCR call failed for {arm}")
        model = RIVOK if arm == "rivok" or (arm == "router_v2" and carrier == "scanner") else MODERN
        require(doc.components.get(f"tesseract:{model}.traineddata") == "sha256:" + MODEL_PINS[model],
                f"Unexpected model receipt for {arm}")
        v6 = doc.components.get("tesseract:line_v6.traineddata")
        if v6 is not None:
            require(arm == "router_v2" and carrier == "scanner" and v6 == "sha256:" + MODEL_PINS["line_v6"],
                    "Unexpected v6 receipt")
        docs[arm] = doc
    return docs


def measure(scorer, translations, reference, text):
    ref, hyp, keep = scorer.page_text(reference["lines"], reference["keep"], translations, text, heocr_layout=True)
    common = scorer.measures(*scorer.project(ref, hyp, keep))
    require(common is not None, "No retained reference characters")
    return {"common": common, "all": scorer.measures(ref, hyp)}


def summarize(rows):
    result = {}
    for stratum in ("scanner", "iphone", "pixel", "pooled"):
        chosen = [r for r in rows if stratum == "pooled" or r["carrier"] == stratum]
        arms = {}
        for arm in ARMS:
            arms[arm] = {}
            for view in ("common", "all"):
                totals = Counter()
                for row in chosen:
                    totals.update(row["arms"][arm][view])
                arms[arm][view] = {**totals, "cer": totals["edits"] / totals["chars"],
                                   "wer": totals["word_edits"] / totals["words"]}
        issues = defaultdict(Counter)
        for row in chosen:
            cluster = issues[row["issue_cluster"]]
            primary = row["arms"]["router_v2"]["common"]
            cluster["chars"] += primary["chars"]
            for arm in ARMS[1:]:
                cluster[arm] += primary["edits"] - row["arms"][arm]["common"]["edits"]
        pairs = {}
        for arm in ARMS[1:]:
            clusters = [issues[issue] for issue in sorted(issues)]
            rng = random.Random(215)
            samples = []
            for _ in range(2000):
                draw = rng.choices(clusters, k=len(clusters))
                samples.append(100 * sum(x[arm] for x in draw) / sum(x["chars"] for x in draw))
            samples.sort()
            pairs[arm] = {"delta_cer_pp": 100 * (arms["router_v2"]["common"]["cer"] - arms[arm]["common"]["cer"]),
                          "issue_bootstrap_95_pp": [samples[49], samples[1950]]}
        result[stratum] = {"captures": len(chosen), "issues": len(issues), "arms": arms, "router_minus_fixed": pairs}
    return result


def score(args):
    freeze, jobs = preflight(args)
    heocr, scorer, revision = runtime(args, freeze)
    translations = scorer.folds()
    private_dir(args.out)
    coverage = {"complete_triads": len(jobs) // len(SLOTS), "packet_triads": 10}
    if args.preflight_only:
        return {"ready": True, "captures": len(jobs), "coverage": coverage, "freeze_sha256": args.freeze_sha}
    rows = []
    for page, slot, carrier, reference, raster in jobs:
        doc = score_one(heocr, read_pinned(raster, page["captures"][slot]["sha256"]), carrier, args)
        rows.append({
            "print_id": page["print_id"], "slot": slot, "carrier": carrier, "issue_cluster": page["issue_cluster"],
            "phone_matches_modern": None if slot == "scanner" else doc["router_v2"].text == doc["modern_v1"].text,
            "v6_used": "tesseract:line_v6.traineddata" in doc["router_v2"].components,
            "arms": {name: measure(scorer, translations, reference, result.text) for name, result in doc.items()},
        })
    data = {
        "schema": "heocr.office.scores.dev215.v1", "split": "dev", "heocr_commit": revision,
        "freeze_sha256": args.freeze_sha, "registration_sha256": args.registration_sha, "model_pins": MODEL_PINS,
        "captures": rows, "coverage": coverage, "summary": summarize(rows),
        "phone_parity": {carrier: sum(r["carrier"] == carrier and r["phone_matches_modern"] for r in rows)
                         for carrier in ("iphone", "pixel")},
    }
    pin = write_receipt(args.out, "scores", data)
    return {"scores_sha256": pin, "captures": len(rows), "coverage": coverage, "phone_parity": data["phone_parity"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    intake = commands.add_parser("register")
    consume = commands.add_parser("score")
    for command, names in (
        (intake, ("print-manifest", "selections", "out")),
        (consume, ("registration", "freeze", "assets", "checkout", "models", "line-v6", "tesseract", "tessdata", "scorer", "out")),
    ):
        for name in names:
            command.add_argument(f"--{name}", type=Path, required=True)
    for name in ("telegram", "photos", "scanner"):
        intake.add_argument(f"--{name}", type=Path)
    for command, names in ((intake, ("print-sha", "selection-sha")), (consume, ("registration-sha", "freeze-sha"))):
        for name in names:
            command.add_argument(f"--{name}", required=True)
    consume.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = register(args) if args.command == "register" else score(args)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(2, f"DEV215 intake or scoring stopped: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
