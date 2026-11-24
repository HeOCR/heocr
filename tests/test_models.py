import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heocr
from heocr import models
from heocr.cli import main
from heocr.ocr import routes
from tests.synth.pdf import PdfPage, lines_block, make_pdf

FAKE = [sys.executable, str(Path(__file__).parent / "synth" / "fake_tesseract.py")]
SCAN = make_pdf([PdfPage(images=[(0, 0, 595, 842)], image_gray=0xFF)])


class ModelRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.tessdata = root / "tessdata"
        (self.tessdata / "configs").mkdir(parents=True)
        (self.tessdata / "configs" / "tsv").write_text("tessedit_create_tsv 1\n", encoding="utf-8")
        (self.tessdata / "heb.traineddata").write_bytes(b"fake heb model")
        self.cache = root / "models"
        self.log = root / "calls.jsonl"
        env = {"FAKE_TESSDATA": str(self.tessdata), "FAKE_LOG": str(self.log), "HEOCR_MODELS": str(self.cache)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TESSDATA_PREFIX", None)

    def recognition_call(self) -> list[str]:
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [c for c in calls if c and not c[0].startswith("--")][0]

    def test_model_by_path_runs_from_a_private_tessdata(self) -> None:
        body = b"a custom model"
        digest = hashlib.sha256(body).hexdigest()
        for name in ("custom_x", "custom-model", "print_v3-snapshot-12"):
            with self.subTest(name=name):
                self.log.unlink(missing_ok=True)
                model = Path(self.tmp.name) / f"{name}.traineddata"
                model.write_bytes(body)
                doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model=str(model))
                self.assertEqual(doc.unread, [])
                call = self.recognition_call()
                self.assertEqual(call[call.index("-l") + 1], name)
                private = Path(call[call.index("--tessdata-dir") + 1])
                self.assertNotEqual(private, self.tessdata)
                self.assertEqual((private / model.name).read_bytes(), body)
                self.assertTrue((private / "configs" / "tsv").is_file())
                self.assertEqual(doc.components[f"tesseract:{model.name}"], f"sha256:{digest}")
                self.assertEqual(doc.blocks[0].ocr["lang"], name)
                payload = doc.to_json()
                self.assertNotIn(str(private), payload)
                self.assertNotIn(self.tmp.name, payload)
                self.assertEqual(doc.to_dict()["options"]["ocr"]["model"], model.name)

    def test_released_name_must_be_downloaded_first(self) -> None:
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="heocr_r8b")
        self.assertEqual(doc.unread[0].reason, "ocr_route_unavailable")
        self.assertIn("run: heocr --download-model heocr_r8b", doc.unread[0].detail)

    def test_released_file_must_match_its_pin(self) -> None:
        self.cache.mkdir()
        (self.cache / "heocr_r8b.traineddata").write_bytes(b"not the released bytes")
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="heocr_r8b")
        self.assertIn("not the released file", doc.unread[0].detail)

    def test_released_model_records_its_doi(self) -> None:
        released = models.ReleasedModel("heocr_test", hashlib.sha256(b"released").hexdigest(), 8, "https://example.invalid/x", "10.0/test", "Apache-2.0")
        self.cache.mkdir()
        (self.cache / "heocr_test.traineddata").write_bytes(b"released")
        with mock.patch.dict(models.MODELS, {"heocr_test": released}):
            doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="heocr_test")
        self.assertEqual(doc.components["tesseract:heocr_test.doi"], "10.0/test")
        self.assertEqual(doc.components["tesseract:heocr_test.licence"], "Apache-2.0")

    def test_installation_without_tsv_config_is_unavailable(self) -> None:
        (self.tessdata / "configs" / "tsv").unlink()
        model = Path(self.tmp.name) / "custom_x.traineddata"
        model.write_bytes(b"m")
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model=str(model))
        self.assertIn("configs/tsv", doc.unread[0].detail)

    def test_the_default_is_the_modern_model(self) -> None:
        # One model for every document, no router. The test package has no bundled files, so stock heb reads
        # instead and the document says so.
        doc = heocr.extract(SCAN, tesseract=FAKE)
        self.assertEqual(doc.to_dict()["options"]["ocr"]["model"], "heocr_modern_v1")
        self.assertNotIn("model_route", doc.blocks[0].ocr)
        self.assertIn("bundled_model_unavailable", doc.flags)
        self.assertEqual(doc.blocks[0].ocr["lang"], "heb")
        self.assertEqual(doc.unread, [])

    def test_short_names_for_the_bundled_models(self) -> None:
        for alias, name in (("modern", "heocr_modern_v1"), ("oldprint", "heocr_oldprint_v1")):
            with self.subTest(alias=alias):
                doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model=alias)
                self.assertEqual(doc.to_dict()["options"]["ocr"]["model"], name)

    def test_auto_is_opt_in(self) -> None:
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="auto")
        self.assertEqual(doc.to_dict()["options"]["ocr"]["model"], "auto")
        self.assertEqual(doc.blocks[0].ocr["model_route"]["mode"], "auto")

    def capture_models(self) -> dict[str, models.ReleasedModel]:
        self.cache.mkdir()
        desktop = b"fake Rivok Full"
        mobile = b"fake modern v1"
        for name, data in (("heb_rivok", desktop), ("heocr_modern_v1", mobile)):
            (self.cache / f"{name}.traineddata").write_bytes(data)
        modern = models.ReleasedModel("heocr_modern_v1", hashlib.sha256(mobile).hexdigest(), len(mobile),
                                      "https://example.invalid/model", "10.0/test", "Apache-2.0", bundled=True)
        rivok = models.ReleasedModel(routes.RIVOK_FULL, hashlib.sha256(desktop).hexdigest(), len(desktop),
                                     "https://example.invalid/rivok", None, "Apache-2.0")
        return {modern.name: modern, rivok.name: rivok}

    def test_capture_route_uses_the_declared_model(self) -> None:
        with mock.patch.dict(models.MODELS, self.capture_models()):
            modern = "heocr_modern_v1"
            choices = (("phone", modern), ("iphone", modern), ("pixel", modern),
                       ("scanner", "heb_rivok"), ("issuer-render", "heb_rivok"))
            for capture, expected in choices:
                with self.subTest(capture=capture):
                    self.log.unlink(missing_ok=True)
                    doc = heocr.extract(SCAN, tesseract=FAKE, ocr_capture_type=capture, check_numbers=False)
                    self.assertEqual(doc.unread, [])
                    call = self.recognition_call()
                    self.assertEqual(call[call.index("-l") + 1], expected)
                    self.assertEqual(doc.options["ocr"]["capture_type"], capture)
                    self.assertEqual(doc.blocks[0].ocr["model_route"], {
                        "mode": "capture", "capture_type": capture, "model": expected, "reason": "declared_capture",
                    })
                    if expected == routes.RIVOK_FULL:
                        self.assertEqual(doc.components["tesseract:heb_rivok.licence"], "Apache-2.0")
                        self.assertNotIn("tesseract:heb_rivok.doi", doc.components)
                    self.assertNotIn(self.tmp.name, doc.to_json())

    def test_v2_classifies_pdf_and_camera_and_accepts_overrides(self) -> None:
        from PIL import ExifTags, Image
        from PIL.TiffImagePlugin import IFDRational

        image = Image.new("RGB", (595, 842), "white")
        image.paste("black", (80, 80, 250, 100))
        exif = Image.Exif()
        exif[ExifTags.IFD.Exif] = {ExifTags.Base.FocalLength: IFDRational(5, 1)}
        jpeg = io.BytesIO()
        image.save(jpeg, format="JPEG", exif=exif)
        png = io.BytesIO()
        image.save(png, format="PNG")
        modern, rivok = "heocr_modern_v1", routes.RIVOK_FULL
        cases = (
            (SCAN, None, "pdf-render", "pdf_container", rivok),
            (SCAN, "pixel", "pixel", "declared_capture", modern),
            (jpeg.getvalue(), None, "camera-photo", "camera_exif", modern),
            (png.getvalue(), "scanner", "scanner", "declared_capture", rivok),
            (png.getvalue(), None, None, "no_capture_evidence", modern),
        )
        with mock.patch.dict(models.MODELS, self.capture_models()):
            for body, declared, capture, reason, model in cases:
                with self.subTest(capture=capture):
                    self.log.unlink(missing_ok=True)
                    doc = heocr.extract(body, tesseract=FAKE, ocr_router="v2", ocr_capture_type=declared,
                                        check_numbers=False)
                    self.assertEqual(doc.unread, [])
                    call = self.recognition_call()
                    self.assertEqual(call[call.index("-l") + 1], model)
                    self.assertEqual(doc.options["ocr"]["router"], {"version": "v2", "reason": reason})
                    self.assertEqual(doc.options["ocr"].get("capture_type"), capture)
                    self.assertEqual(doc.blocks[0].ocr.get("model_route"), None if capture is None else {
                        "mode": "capture", "capture_type": capture, "reason": reason, "model": model,
                    })
                    self.assertEqual(heocr.validate(doc.to_dict()), [])

            self.log.unlink(missing_ok=True)
            default = heocr.extract(SCAN, tesseract=FAKE, check_numbers=False)
            call = self.recognition_call()
            self.assertEqual(call[call.index("-l") + 1], modern)
            self.assertNotIn("router", default.options["ocr"])

    def test_v2_preserves_text_layers_and_checks_settings(self) -> None:
        source = make_pdf([PdfPage(lines=lines_block(["שלום עולם"]))])
        with mock.patch.dict(models.MODELS, self.capture_models()):
            native = heocr.extract(source, tesseract=FAKE, ocr_router="v2", check_numbers=False)
            self.assertEqual(native.text, heocr.extract(source, ocr=False).text)
            self.assertFalse(self.log.exists())
            forced = heocr.extract(source, tesseract=FAKE, ocr_router="v2", force_ocr=True, check_numbers=False)
            call = self.recognition_call()
            self.assertEqual(call[call.index("-l") + 1], routes.RIVOK_FULL)
            self.assertIn("ocr_forced", forced.pages[0].flags)
        for kwargs in ({"ocr_model": "auto"}, {"ocr_lang": "heb+eng"}, {"ocr": False}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "needs Hebrew Tesseract"):
                heocr.extract(SCAN, ocr_router="v2", **kwargs)
        with self.assertRaisesRegex(ValueError, "must be 'v2'"):
            heocr.extract(SCAN, ocr_router="unknown")

    def test_line_v6_rereads_prose_and_keeps_layout(self) -> None:
        self.cache.mkdir()
        rivok_bytes, v6_bytes = b"Rivok fixture", b"v6 fixture"
        rivok_sha = hashlib.sha256(rivok_bytes).hexdigest()
        v6_sha = hashlib.sha256(v6_bytes).hexdigest()
        (self.cache / "heb_rivok.traineddata").write_bytes(rivok_bytes)
        model = Path(self.tmp.name) / "v6.traineddata"
        model.write_bytes(v6_bytes)
        rivok = models.ReleasedModel("heb_rivok", rivok_sha, len(rivok_bytes),
                                     "https://example.invalid/rivok", None, "Apache-2.0")
        options = {"tesseract": FAKE, "ocr_capture_type": "scanner", "check_numbers": False, "repairs": False}
        with mock.patch.dict(models.MODELS, {rivok.name: rivok}), mock.patch(
            "heocr.ocr.line_models.V6_SHA256", v6_sha
        ), mock.patch("heocr.ocr.line_models._read_line", return_value=("שלום כולם", 95)) as reread:
            base = heocr.extract(SCAN, **options)
            self.assertFalse(reread.called)
            opted_in = heocr.extract(SCAN, **options, ocr_line_v6=model)
            self.assertTrue(reread.called)
            routed = heocr.extract(SCAN, tesseract=FAKE, ocr_router="v2", ocr_line_v6=model,
                                   check_numbers=False, repairs=False)
            self.assertEqual(routed.blocks[0].ocr["line_router"], opted_in.blocks[0].ocr["line_router"])
            reread.reset_mock()
            phone = heocr.extract(SCAN, tesseract=FAKE, ocr_router="v2", ocr_capture_type="phone",
                                  ocr_line_v6=model, check_numbers=False)
            self.assertNotIn("line_v6_sha256", phone.options["ocr"])
            reread.assert_not_called()
            model.write_bytes(b"wrong model")
            fallback = heocr.extract(SCAN, **options, ocr_line_v6=model)

        self.assertEqual(opted_in.blocks[0].lines[0].text, "שלום כולם")
        self.assertEqual(opted_in.blocks[0].ocr["line_router"]["changes"],
                         [{"line": 1, "selection": "rivok_agreement"}])
        self.assertEqual(opted_in.components["tesseract:line_v6.traineddata"], f"sha256:{v6_sha}")
        self.assertEqual(opted_in.components["tesseract:heb_rivok.traineddata"], f"sha256:{rivok_sha}")
        for old, new in zip(base.blocks, opted_in.blocks, strict=True):
            self.assertEqual(old.bbox, new.bbox)
            self.assertEqual([line.bbox for line in old.lines], [line.bbox for line in new.lines])
        self.assertEqual(base.text, fallback.text)
        self.assertIn("ocr_line_v6_unavailable", fallback.flags)
        self.assertNotIn(self.tmp.name, opted_in.to_json())
        self.assertEqual(heocr.validate(opted_in.to_dict()), [])

    def test_line_router_keeps_original_digits_and_complete_lines(self) -> None:
        from heocr.ocr.line_models import choose_line

        self.assertIsNone(choose_line("שלום 123", ("שלום 124", 98), ("שלום 124", 98)))
        self.assertIsNone(choose_line("שלום עולם", ("שלום", 98), ("שלום", 98)))
        self.assertIsNone(choose_line("שלום עולם", ("שלום כולם", 97), ("שלום כולם", 50)))

    def test_line_v6_needs_explicit_rivok_and_a_traineddata_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs Rivok Full"):
            heocr.extract(SCAN, ocr_capture_type="phone", ocr_line_v6="v6.traineddata")
        with self.assertRaisesRegex(ValueError, "needs a local"):
            heocr.extract(SCAN, ocr_model="heb_rivok", ocr_line_v6="v6")

    def test_capture_route_reports_missing_or_mismatched_models_without_ocr(self) -> None:
        for capture in ("phone", "scanner"):
            with self.subTest(capture=capture):
                doc = heocr.extract(SCAN, tesseract=FAKE, ocr_capture_type=capture)
                self.assertEqual(doc.unread[0].reason, "ocr_route_unavailable")
        self.cache.mkdir()
        (self.cache / "heb_rivok.traineddata").write_bytes(b"different model")
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_capture_type="issuer-render")
        self.assertIn("sha256 differs", doc.unread[0].detail)
        routed = heocr.extract(SCAN, tesseract=FAKE, ocr_router="v2")
        self.assertIn("sha256 differs", routed.unread[0].detail)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(call[0].startswith("--") for call in calls))

    def test_capture_route_rejects_conflicting_settings(self) -> None:
        for kwargs in ({"ocr_model": "auto"}, {"ocr_lang": "heb+eng"}, {"ocr": False}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "needs Hebrew Tesseract"):
                heocr.extract(SCAN, ocr_capture_type="phone", **kwargs)
        with self.assertRaisesRegex(ValueError, "must be one of"):
            heocr.extract(SCAN, ocr_capture_type="unknown")

    def test_heb_and_stock_read_with_stock_tesseract(self) -> None:
        for model in ("heb", "stock", None):
            with self.subTest(model=model):
                doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model=model)
                self.assertEqual(doc.blocks[0].ocr["lang"], "heb")
                self.assertNotIn("model_route", doc.blocks[0].ocr)
                self.assertNotIn("model", doc.to_dict()["options"]["ocr"])
                self.assertNotIn("routed_model_unavailable", doc.flags)
                self.assertNotIn("bundled_model_unavailable", doc.flags)

    def test_another_language_list_reads_with_stock(self) -> None:
        # The default and auto use Hebrew models: a list such as heb+eng is not replaced by one of them.
        # (The fake engine has heb only, so nothing is read here; what matters is the setting.)
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_lang="heb+eng")
        self.assertEqual(doc.to_dict()["options"]["ocr"]["lang"], "heb+eng")
        self.assertNotIn("model", doc.to_dict()["options"]["ocr"])
        self.assertNotIn("routed_model_unavailable", doc.flags)
        self.assertNotIn("bundled_model_unavailable", doc.flags)
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_lang="heb+eng", ocr_model="auto")
        self.assertNotIn("model", doc.to_dict()["options"]["ocr"])


class DownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HEOCR_MODELS": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def released(self, data: bytes) -> models.ReleasedModel:
        return models.ReleasedModel("heocr_test", hashlib.sha256(data).hexdigest(), len(data), "https://example.invalid/x", "10.0/x", "Apache-2.0")

    def test_download_checks_the_pin(self) -> None:
        with mock.patch.dict(models.MODELS, {"heocr_test": self.released(b"good bytes")}), mock.patch(
            "urllib.request.urlopen", return_value=io.BytesIO(b"good bytes")
        ):
            path = models.download("heocr_test")
        self.assertEqual(path.read_bytes(), b"good bytes")

    def test_mismatch_keeps_nothing(self) -> None:
        with mock.patch.dict(models.MODELS, {"heocr_test": self.released(b"good bytes")}), mock.patch(
            "urllib.request.urlopen", return_value=io.BytesIO(b"tampered")
        ):
            with self.assertRaises(models.ModelError):
                models.download("heocr_test")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_registry_pins(self) -> None:
        r8b = models.MODELS["heocr_r8b"]
        self.assertEqual(r8b.sha256, "8cb22dbeb195c3c0ffbe9c66cf956d8c2052d3b1c4a872234a85c975c4526117")
        self.assertEqual((r8b.size, r8b.licence, r8b.doi), (3_633_477, "Apache-2.0", "10.5281/zenodo.24372346"))
        rivok = models.MODELS[routes.RIVOK_FULL]
        self.assertEqual(rivok.sha256, "09ef1830f3393c590791abf9c18deca14450c8e5d3b556249d63b0183cf7a426")
        self.assertEqual((rivok.size, rivok.licence, rivok.bundled), (5_678_607, "Apache-2.0", False))
        self.assertEqual(rivok.url, "https://huggingface.co/Rivok/tesseract-heb-rivok/resolve/"
                         "9253cacf097ab396a2fc36d633737c43af5c28a3/tessdata/heb_rivok.traineddata")

    def test_cli(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["--download-model", "no_such_model"]), 1)
        self.assertIn("unknown model", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main([])


if __name__ == "__main__":
    unittest.main()


class BundledAndRoutesTest(unittest.TestCase):
    """D6 plumbing: models bundled in the package, and the route table for ``ocr_model="auto"``."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache, self.bundled = root / "cache", root / "bundled"
        self.cache.mkdir()
        self.bundled.mkdir()
        body = b"an invented model file"
        self.model = models.ReleasedModel("heocr_test", hashlib.sha256(body).hexdigest(), len(body), "https://example.invalid/m", "10.0/none", "Apache-2.0", bundled=True)
        self.body = body
        for patch in (mock.patch.dict(os.environ, {"HEOCR_MODELS": str(self.cache)}),
                      mock.patch.dict(models.MODELS, {"heocr_test": self.model}),
                      mock.patch("heocr.models.bundled_dir", return_value=self.bundled)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_bundled_model_resolves_without_a_download(self) -> None:
        with self.assertRaises(models.ModelError):
            models.resolve("heocr_test")
        (self.bundled / "heocr_test.traineddata").write_bytes(self.body)
        path, name, released = models.resolve("heocr_test")
        self.assertEqual((path.parent, name, released), (self.bundled, "heocr_test", self.model))

    def test_the_download_cache_comes_before_the_bundled_copy(self) -> None:
        (self.bundled / "heocr_test.traineddata").write_bytes(self.body)
        (self.cache / "heocr_test.traineddata").write_bytes(self.body)
        self.assertEqual(models.resolve("heocr_test")[0].parent, self.cache)

    def test_a_bundled_copy_must_match_its_pin(self) -> None:
        (self.bundled / "heocr_test.traineddata").write_bytes(b"not the released file")
        with self.assertRaises(models.ModelError):
            models.resolve("heocr_test")

    def test_the_route_table_names_bundled_models_of_its_domains(self) -> None:
        from heocr.ocr import routes

        self.assertEqual(set(routes.ROUTES), set(routes.DOMAINS))
        self.assertIn(routes.DEFAULT_DOMAIN, routes.DOMAINS)
        for domain in routes.DOMAINS:
            model = models.MODELS[routes.model_for(domain)]
            self.assertEqual((model.domain, model.bundled, model.licence), (domain, True, "Apache-2.0"))
        with self.assertRaises(ValueError):
            routes.model_for("handwriting")


class AutoRouteTest(unittest.TestCase):
    """``ocr_model="auto"`` (D6): one model per document, from the route table and, when it offers more than
    one, a probe of a few of the first OCR page's lines. The text and the models are invented."""

    setUp = ModelRouteTest.setUp  # the same fake Tesseract and tessdata, without rerunning that class's tests

    def page_tsv(self, conf: str) -> dict:
        rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
        for ln, words in enumerate([["שורה", "ראשונה", "של", "דף"], ["שורה", "שנייה", "של", "דף"], ["שורה", "שלישית", "של", "דף"]], 1):
            for n, w in enumerate(words, 1):
                rows.append(f"5\t1\t1\t1\t{ln}\t{n}\t{1800 - 300 * n}\t{100 * ln}\t250\t60\t{conf}\t{w}")
        path = Path(self.tmp.name) / "page.tsv"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return {"FAKE_TSV": str(path)}

    def with_old_model(self):
        body = b"an invented old-print model"
        released = models.ReleasedModel("heocr_oldt", hashlib.sha256(body).hexdigest(), len(body), "https://example.invalid/o", "10.0/none", "Apache-2.0", domain="old")
        self.cache.mkdir(exist_ok=True)
        (self.cache / "heocr_oldt.traineddata").write_bytes(body)
        return mock.patch.dict(models.MODELS, {"heocr_oldt": released}), mock.patch.dict("heocr.ocr.routes.ROUTES", {"modern": None, "old": "heocr_oldt"})

    def recognition_langs(self) -> list[tuple[str, str]]:
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [(c[c.index("-l") + 1], c[c.index("--psm") + 1]) for c in calls if c and "-l" in c and "--psm" in c]

    def test_one_candidate_needs_no_probe(self) -> None:
        with mock.patch.dict(os.environ, self.page_tsv("80")), mock.patch.dict("heocr.ocr.routes.ROUTES", {"modern": None, "old": None}):
            doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="auto")
        self.assertEqual(doc.options["ocr"]["model"], "auto")
        self.assertTrue(doc.blocks)
        self.assertEqual({tuple(sorted(b.ocr["model_route"].items())) for b in doc.blocks}, {(("domain", "modern"), ("mode", "auto"), ("reason", "single_candidate"))})
        self.assertEqual({lang for lang, _ in self.recognition_langs()}, {"heb"})
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_the_probe_switches_to_a_clearly_better_model(self) -> None:
        a, b = self.with_old_model()
        env = {**self.page_tsv("60"), "FAKE_LINE_HEOCR_OLDT": "שורה ישנה של דף", "FAKE_CONF_HEOCR_OLDT": "95"}
        with a, b, mock.patch.dict(os.environ, env):
            doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="auto")
        route = doc.blocks[0].ocr["model_route"]
        self.assertEqual((route["domain"], route["reason"]), ("old", "probe"))
        self.assertGreater(route["scores"]["old"], route["scores"]["modern"])
        self.assertIn(("heocr_oldt", "3"), self.recognition_langs())  # the deciding page is read again by the chosen model
        self.assertEqual(doc.blocks[0].ocr["lang"], "heocr_oldt")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_routed_model_that_is_not_here_falls_back_to_stock_and_says_so(self) -> None:
        # The table names the released models; with neither bundled nor downloaded, auto reads with stock heb.
        with mock.patch.dict(os.environ, self.page_tsv("80")), mock.patch("heocr.models.bundled_dir", return_value=self.cache / "none"):
            doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="auto")
        self.assertIn("routed_model_unavailable", doc.flags)
        self.assertTrue(doc.blocks)
        self.assertEqual(doc.blocks[0].ocr["lang"], "heb")
        self.assertEqual(doc.blocks[0].ocr["model_route"]["unavailable"], "heocr_modern_v1")
        self.assertEqual(heocr.validate(doc.to_dict()), [])

    def test_a_close_call_keeps_the_fallback(self) -> None:
        a, b = self.with_old_model()
        env = {**self.page_tsv("60"), "FAKE_LINE_HEOCR_OLDT": "שורה ישנה של דף", "FAKE_CONF_HEOCR_OLDT": "62"}
        with a, b, mock.patch.dict(os.environ, env):
            doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model="auto")
        route = doc.blocks[0].ocr["model_route"]
        self.assertEqual((route["domain"], route["reason"]), ("modern", "probe_unsure"))
        self.assertNotIn(("heocr_oldt", "3"), self.recognition_langs())
        self.assertEqual(doc.blocks[0].ocr["lang"], "heb")
