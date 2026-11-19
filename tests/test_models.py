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
from tests.synth.pdf import PdfPage, make_pdf

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
        model = Path(self.tmp.name) / "custom_x.traineddata"
        model.write_bytes(b"a custom model")
        doc = heocr.extract(SCAN, tesseract=FAKE, ocr_model=str(model))
        call = self.recognition_call()
        self.assertEqual(call[call.index("-l") + 1], "custom_x")
        private = Path(call[call.index("--tessdata-dir") + 1])
        self.assertNotEqual(private, self.tessdata)
        self.assertTrue((private / "custom_x.traineddata").is_file())
        self.assertTrue((private / "configs" / "tsv").is_file())
        self.assertEqual(doc.components["tesseract:custom_x.traineddata"], "sha256:" + hashlib.sha256(b"a custom model").hexdigest())
        self.assertEqual(doc.blocks[0].ocr["lang"], "custom_x")
        payload = doc.to_json()
        self.assertNotIn(str(private), payload)
        self.assertNotIn(self.tmp.name, payload)
        self.assertEqual(doc.to_dict()["options"]["ocr"]["model"], "custom_x.traineddata")

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

    def test_default_stays_stock_heb(self) -> None:
        doc = heocr.extract(SCAN, tesseract=FAKE)
        self.assertEqual(doc.blocks[0].ocr["lang"], "heb")
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
        self.assertEqual((r8b.size, r8b.licence, r8b.doi), (3_633_477, "Apache-2.0", "10.5281/zenodo.24156688"))
        self.assertTrue(all(m.url.startswith("https://huggingface.co/HeOCR/") for m in models.MODELS.values()))

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
