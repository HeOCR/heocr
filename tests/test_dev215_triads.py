"""Offline tests for the Thursday intake and score handoff."""
import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import dev215_triads as triads


class TriadToolTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()
        self.intake = self.root / "intake"
        self.print_ids = [f"DEV215-{n:02d}" for n in range(1, 11)]
        self.print_manifest, self.print_sha = self.write("print.json", {
            "schema": "heocr.office.print.dev215.v1", "split": "dev",
            "freeze_sha256": "a" * 64, "pdf_sha256": "b" * 64,
            "pages": [{"print_id": p, "dev_source_id": f"source-{p}", "capture_slots": list(triads.SLOTS),
                       "source_pdf_sha256": "c" * 64, "source_raster_sha256": "d" * 64} for p in self.print_ids],
        })
        self.devices = {slot: {"id": slot, "type": carrier} for slot, carrier in
                        zip(triads.SLOTS, ("scanner", "pixel", "iphone"), strict=True)}
        self.roots = {name: self.root / name for name in ("telegram", "photos", "scanner")}
        for directory in self.roots.values():
            directory.mkdir()

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data))
        return path, triads.hash_file(path)

    def row(self, print_id, slot, provider, name, page_index=0):
        return {"print_id": print_id, "print_instance": f"sheet-{print_id}", "slot": slot,
                "provider": provider, "file": name, "page_index": page_index, "rotation": 0, "original": True}

    def make_registration(self, rows):
        selection, selection_sha = self.write("selection.json", {
            "schema": "heocr.office.selection.dev215.v1", "split": "dev",
            "print_manifest_sha256": self.print_sha, "devices": self.devices, "captures": rows,
        })
        args = argparse.Namespace(print_manifest=self.print_manifest, print_sha=self.print_sha, out=self.intake,
                                  selections=selection, selection_sha=selection_sha, **self.roots)
        result = triads.register(args)
        return args, result, self.intake / f"registration-{result['registration_sha256']}.json"

    def test_originals_pull_by_selection_and_rerun_without_rewriting(self):
        print_id = self.print_ids[0]
        device_a = b"\xff\xd8\xffFake Telegram document"
        device_b = b"\xff\xd8\xffFake Photos original"
        (self.roots["telegram"] / "a.jpg").write_bytes(device_a)
        (self.roots["photos"] / "b.jpg").write_bytes(device_b)
        rows = [self.row(print_id, "phone-a", "telegram", "a.jpg"), self.row(print_id, "phone-b", "photos", "b.jpg")]
        args, result, path = self.make_registration(rows)
        self.assertEqual((result["captures"], len(result["missing"])), (2, 28))
        self.assertEqual(triads.register(args), result)
        receipt = json.loads(path.read_bytes())
        for row, source in zip(receipt["captures"], (device_a, device_b), strict=True):
            self.assertEqual(row["raw_sha256"], triads.digest(source))
            self.assertEqual((self.intake / row["raw_file"]).read_bytes(), source)
            self.assertNotIn('"a.jpg"', path.read_text())
            self.assertNotIn('"b.jpg"', path.read_text())

    def test_duplicate_capture_page_and_out_of_scope_paths_are_rejected(self):
        for directory in self.roots.values():
            (directory / "a.jpg").write_bytes(b"\xff\xd8\xffSame fake original")
        a = self.row(self.print_ids[0], "phone-a", "telegram", "a.jpg")
        b = self.row(self.print_ids[0], "phone-b", "photos", "a.jpg")
        with self.assertRaisesRegex(ValueError, "assigned to multiple slots"):
            self.make_registration([a, b])
        with self.assertRaisesRegex(ValueError, "relative asset path"):
            self.make_registration([{**a, "file": "../photos/a.jpg"}])
        self.assertFalse(self.intake.exists())

    def test_prepared_raster_rejects_unmasked_witness(self):
        import io

        raster = Image.new("RGB", (8, 8), "white")
        raster.putpixel((1, 1), (0, 0, 0))
        body = io.BytesIO()
        raster.save(body, format="PNG", dpi=(144, 144))
        with self.assertRaisesRegex(ValueError, "Witness mask is not white"):
            triads.checked_raster(body.getvalue(), {
                "size": [8, 8], "witness_boxes": [[0, 0, 2, 2]], "witness_outside_crop": False,
            })

    def test_full_registered_packet_scores_exactly_three_arms_per_capture(self):
        self.assets = self.root / "assets"
        self.assets.mkdir()
        scan = self.roots["scanner"] / "stack.pdf"
        scan.write_bytes(b"%PDF-1.5\nOne scanner file with operator-pinned page indices")
        rows = []
        pages = []
        for index, print_id in enumerate(self.print_ids):
            ref_file, ref_sha = self.write(f"assets/{print_id}.json", {
                "schema": "heocr.office.reference.dev215.v1", "split": "dev", "print_id": print_id,
                "source_id": f"source-{print_id}", "lines": ["שלום עולם"], "keep": [True],
            })
            assets = {}
            for offset, (slot, provider) in enumerate(zip(triads.SLOTS, ("scanner", "telegram", "photos"), strict=True)):
                if slot == "scanner":
                    path = scan
                    page_index = index
                else:
                    path = self.roots[provider] / f"{print_id}.jpg"
                    path.write_bytes(b"\xff\xd8\xff" + f"{slot}-{index}".encode())
                    page_index = 0
                rows.append(self.row(print_id, slot, provider, path.name, page_index))
                png = self.assets / f"{print_id}-{slot}.png"
                raster = Image.new("RGB", (8, 8), "white")
                raster.putpixel((4, 4), (index * 3 + offset, 0, 0))
                raster.save(png, dpi=(144, 144))
                assets[slot] = {
                    "file": png.name, "sha256": triads.hash_file(png), "size": [8, 8],
                    "raw_sha256": triads.hash_file(path), "page_index": page_index, "rotation": 0,
                    "witness_boxes": [[0, 0, 2, 2]], "witness_outside_crop": False,
                    "geometry_receipt_sha256": "e" * 64,
                }
            pages.append({"print_id": print_id, "source_id": f"source-{print_id}", "issue_cluster": f"i{index // 2}",
                          "reference": ref_file.name, "reference_sha256": ref_sha, "captures": assets})
        _, receipt, registration = self.make_registration(rows)
        self.assertEqual(len(list((self.intake / "raw").iterdir())), 21)
        freeze, freeze_sha = self.write("freeze.json", {
            "schema": "heocr.office.score-freeze.dev215.v1", "split": "dev", "heocr_commit": "h" * 40,
            "registration_sha256": receipt["registration_sha256"], "source_freeze_sha256": "a" * 64,
            "lineage_receipt_sha256": "f" * 64, "review_receipt_sha256": "0" * 64, "pages": pages,
        })
        args = argparse.Namespace(registration=registration, registration_sha=receipt["registration_sha256"],
                                  freeze=freeze, freeze_sha=freeze_sha, assets=self.assets, out=self.root / "scores",
                                  preflight_only=False, tesseract=self.root / "fake-tesseract", line_v6=self.root / "v6")
        calls = []

        def extract(body, **options):
            calls.append(options)
            model = options.get("ocr_model") or (triads.RIVOK if options["ocr_capture_type"] == "scanner" else triads.MODERN)
            components = {f"tesseract:{model}.traineddata": "sha256:" + triads.MODEL_PINS[model]}
            if options.get("ocr_line_v6"):
                components["tesseract:line_v6.traineddata"] = "sha256:" + triads.MODEL_PINS["line_v6"]
            return SimpleNamespace(pages=[SimpleNamespace(route="ocr")], unread=[], flags=[], components=components,
                                   text="שלום עולם")

        scorer = SimpleNamespace(
            folds=lambda: {},
            page_text=lambda lines, keep, translations, text, **kwargs: (lines[0], text, keep),
            project=lambda ref, hyp, keep: (ref, hyp),
            measures=lambda ref, hyp: {"chars": len(ref), "edits": int(ref != hyp), "words": len(ref.split()),
                                       "word_edits": 0, "ref_numbers": 0, "missed_numbers": 0},
        )
        with patch.object(triads, "runtime", return_value=(SimpleNamespace(extract=extract), scorer, "h" * 40)):
            result = triads.score(args)
        self.assertEqual(result["phone_parity"], {"iphone": 10, "pixel": 10})
        self.assertEqual(len(calls), 90)
        self.assertEqual(sum(bool(opts.get("ocr_line_v6")) for opts in calls), 10)
        self.assertEqual({opts.get("ocr_capture_type") for opts in calls if opts.get("ocr_router")},
                         {"scanner", "iphone", "pixel"})
        output = args.out / f"scores-{result['scores_sha256']}.json"
        summary = json.loads(output.read_bytes())["summary"]
        self.assertEqual(summary["pooled"]["captures"], 30)
        self.assertEqual(summary["pooled"]["issues"], 5)
        self.assertEqual(summary["pixel"]["router_minus_fixed"]["modern_v1"]["issue_bootstrap_95_pp"], [0, 0])
        self.assertNotIn("שלום", output.read_text())
        _, partial, partial_path = self.make_registration(rows[:4])
        partial_freeze = json.loads(freeze.read_bytes())
        partial_freeze["pages"] = pages[:1]
        partial_freeze["registration_sha256"] = partial["registration_sha256"]
        staged, staged_sha = self.write("partial-freeze.json", partial_freeze)
        partial_args = argparse.Namespace(**{**vars(args), "registration": partial_path,
                                             "registration_sha": partial["registration_sha256"],
                                             "freeze": staged, "freeze_sha": staged_sha})
        self.assertEqual(len(triads.preflight(partial_args)[1]), 3)
        # The asset hash rejects altered pixels before any reader call.
        (self.assets / pages[0]["captures"]["scanner"]["file"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            triads.preflight(args)
