import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from PIL import Image, ImageDraw, PngImagePlugin

from heocr import extract
from heocr.cli import main
from heocr.ocr import hybrid
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable
from tests.synth.pdf import PdfPage, lines_block, make_pdf

A_SHA = "17655f3807bcf8b39a10fa12db3e01040793b3e9204f831a9699d9eec8040f69"
B_SHA = "a645518afc01f5ffb8221db2cb45dfd291146944f0372df2a434f4f45b0b9c89"
KEY = "mock-anthropic-key"
HOSTED = "טקסט מהתמונה\nמספר 123"
LOCAL = "טקסט מקומי"


def raster(frames=1):
    image = Image.new("RGB", (640, 640), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 210, 50), fill=(10, 20, 130))
    out = io.BytesIO()
    if frames > 1:
        image.save(out, format="TIFF", save_all=True, append_images=[image] * (frames - 1))
    else:
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("private note", "should never leave the document")
        image.save(out, format="PNG", pnginfo=metadata)
    return out.getvalue()


def ok_reply(**overrides):
    return {
        "model": "claude-haiku-5-5", "stop_reason": "end_turn",
        "usage": {"input_tokens": 1000, "output_tokens": 200},
        "content": [{"type": "thinking"}, {"type": "text", "text": HOSTED}],
        **overrides,
    }


class HybridTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)
        network = mock.patch.object(hybrid, "build_opener")
        self.build_opener = network.start()
        self.addCleanup(network.stop)
        self.open = self.build_opener.return_value.open
        self.open.side_effect = lambda *args, **kwargs: io.BytesIO(json.dumps(ok_reply()).encode())
        self.body = raster()
        self.local = mock.Mock(psm=3, lang="heb", failures=0, route=None, version="tesseract 5.5.3")
        self.local.provenance.return_value = {}
        self.local.recognize.return_value = OcrResult([OcrLine(LOCAL, [20, 20, 211, 51])])

    def extract(self, body=None, **kwargs):
        return extract(self.body if body is None else body, ocr_model="stock",
                       repairs=False, check_numbers=False, **kwargs)

    def request_content(self, call=0):
        request = self.open.call_args_list[call].args[0]
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_header("X-api-key"), KEY)
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "claude-haiku-5-5")
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(len(payload["messages"]), 1)
        self.assertIsNone(self.build_opener.call_args.args[0].redirect_request(
            request, None, 307, "", {}, "https://example.invalid"))
        return payload["messages"][0]["content"]

    def test_default_has_no_egress_even_with_key(self):
        with mock.patch.object(hybrid.Tesseract, "find", return_value=self.local):
            doc = self.extract()
        self.build_opener.assert_not_called()
        self.assertEqual(doc.text, LOCAL + "\n")
        self.assertNotIn("hybrid", doc.options["ocr"])

    def test_modern_dispatch_has_exact_prompt_and_stripped_rgb(self):
        with mock.patch.object(hybrid.Tesseract, "find") as local:
            doc = self.extract(ocr_hybrid="haiku")
        local.assert_not_called()
        prompt, image = self.request_content()
        self.assertEqual(sha256(prompt["text"].encode()).hexdigest(), A_SHA)
        self.assertEqual(image["source"]["media_type"], "image/png")
        with Image.open(io.BytesIO(base64.b64decode(image["source"]["data"]))) as decoded:
            self.assertEqual(decoded.getpixel((25, 25)), (10, 20, 130))
            self.assertEqual(decoded.info, {})
        self.assertEqual(doc.text, HOSTED + "\n")
        self.assertEqual(doc.blocks[0].ocr["status"], "unverified")
        self.assertIsNone(doc.blocks[0].bbox)
        self.assertEqual(doc.pages[0].signals["ocr_hybrid"], "hosted")
        self.assertEqual(doc.pages[0].signals["ocr_hybrid_usage"], {
            "input_tokens": 1000, "output_tokens": 200, "list_price_usd": "0.0002",
        })
        self.assertEqual(doc.options["ocr"]["hybrid"]["prompt_sha256"], A_SHA)
        self.assertEqual(Decimal(doc.options["ocr"]["hybrid"]["budget_used_usd"]), Decimal("0.0002"))
        self.assertIsNone(doc._hybrid)
        self.assertNotIn(KEY, doc.to_json())

    def test_old_print_sends_exact_prompt_and_pinned_local_draft(self):
        self.local.models = {"heocr_r8b": "a" * 64}
        self.local.version = "tesseract 5.5.3"
        self.local.executable_sha256 = "b" * 64
        self.local.recognize.return_value = OcrResult([
            OcrLine("דף", [20, 20, 211, 51]), OcrLine("ישן", [20, 80, 211, 110]),
        ])
        with mock.patch.object(hybrid.Tesseract, "find", return_value=self.local) as find:
            doc = self.extract(ocr_hybrid="haiku", ocr_hybrid_old_print=True)
        find.assert_called_once_with(None, model="heocr_r8b", psm=3)
        self.assertEqual(self.local.recognize.call_args.kwargs, {"dpi": 200})
        self.assertEqual(self.local.recognize.call_args.args[0].mode, "RGB")
        prompt, _, seed = self.request_content()
        self.assertEqual(sha256(prompt["text"].encode()).hexdigest(), B_SHA)
        self.assertEqual(seed["text"], "Local OCR follows:\nדף\nישן")
        self.assertEqual(doc.blocks[0].ocr["seed_model_sha256"], "a" * 64)
        self.assertEqual(doc.text, HOSTED + "\n")
        doc.to_dict()

    def test_missing_key_seed_and_budget_use_the_ordinary_pipeline(self):
        cases = (
            ("missing_api_key", "", {}, [self.local]),
            ("cost_cap", KEY, {"ocr_hybrid_max_usd": "0"}, [self.local]),
            ("local_seed_unavailable", KEY, {"ocr_hybrid_old_print": True}, [OcrUnavailable("seed missing"), self.local]),
        )
        for reason, key, options, engines in cases:
            with self.subTest(reason=reason), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": key}):
                with mock.patch.object(hybrid.Tesseract, "find", side_effect=engines):
                    doc = self.extract(ocr_hybrid="haiku", **options)
                self.assertEqual(doc.text, LOCAL + "\n")
                self.assertEqual(doc.pages[0].signals["ocr_hybrid"], reason)
                self.assertEqual(doc.blocks[0].ocr["engine"], "tesseract")
        self.build_opener.assert_not_called()

    def test_one_budget_is_shared_across_frames_and_reset_for_each_document(self):
        cap = str(hybrid.REQUEST_RESERVE_USD + Decimal("0.0002"))
        with mock.patch.object(hybrid.Tesseract, "find", return_value=self.local):
            docs = [self.extract(raster(3), ocr_hybrid="haiku", ocr_hybrid_max_usd=cap) for _ in range(2)]
        self.assertEqual(self.open.call_count, 4)
        for doc in docs:
            self.assertEqual([page.signals["ocr_hybrid"] for page in doc.pages], ["hosted", "hosted", "cost_cap"])
            self.assertEqual(Decimal(doc.options["ocr"]["hybrid"]["budget_used_usd"]), Decimal("0.0004"))
            self.assertEqual(doc.blocks[-1].text, LOCAL)
            doc.to_dict()

    def test_unknown_charge_is_reserved_and_api_disabled_on_failure(self):
        error = HTTPError(hybrid.API_URL, 503, "unavailable", {}, io.BytesIO(b"private provider error"))
        self.open.side_effect = error
        body = raster(2)
        with mock.patch.object(hybrid.Tesseract, "find", return_value=self.local):
            usual = self.extract(body)
            doc = self.extract(body, ocr_hybrid="haiku")
        self.open.assert_called_once()
        self.assertEqual([page.signals["ocr_hybrid"] for page in doc.pages],
                         ["api_error", "api_disabled_after_error"])
        self.assertEqual(Decimal(doc.options["ocr"]["hybrid"]["budget_used_usd"]), hybrid.REQUEST_RESERVE_USD)
        self.assertEqual(doc.blocks, usual.blocks)
        self.assertEqual(doc.unread, usual.unread)
        self.assertNotIn("private provider error", doc.to_json())

    def test_incomplete_transcript_keeps_local_but_charges_reported_usage(self):
        reply = ok_reply(stop_reason="max_tokens", usage={"input_tokens": 101_000, "output_tokens": 8192})
        self.open.side_effect = None
        self.open.return_value = io.BytesIO(json.dumps(reply).encode())
        with mock.patch.object(hybrid.Tesseract, "find", return_value=self.local):
            doc = self.extract(ocr_hybrid="haiku")
        self.assertEqual(doc.text, LOCAL + "\n")
        self.assertEqual(doc.pages[0].signals["ocr_hybrid"], "no_complete_transcript")
        self.assertEqual(Decimal(doc.options["ocr"]["hybrid"]["budget_used_usd"]), Decimal("0.07098"))

    def test_wrong_model_and_invalid_usage_keep_local(self):
        for reply in [
            ok_reply(model="other"),
            ok_reply(usage={"input_tokens": 100, "output_tokens": 8193}),
            ok_reply(usage={"input_tokens": 100, "output_tokens": 1, "cache_creation_input_tokens": 10}),
        ]:
            with self.subTest(reply=reply), mock.patch.object(hybrid.Tesseract, "find", return_value=self.local):
                self.open.side_effect = None
                self.open.return_value = io.BytesIO(json.dumps(reply).encode())
                doc = self.extract(ocr_hybrid="haiku")
                self.assertEqual(doc.text, LOCAL + "\n")
                self.assertEqual(doc.pages[0].signals["ocr_hybrid"], "api_error")
                self.assertEqual(Decimal(doc.options["ocr"]["hybrid"]["budget_used_usd"]), hybrid.REQUEST_RESERVE_USD)

    def test_pdf_layer_stays_local_unless_force_ocr_is_requested(self):
        pdf = make_pdf([PdfPage(lines=lines_block(["עברית 123"]))])
        regular = self.extract(pdf, ocr_hybrid="haiku")
        self.open.assert_not_called()
        self.assertIn("123", regular.text)
        doc = self.extract(pdf, ocr_hybrid="haiku", force_ocr=True)
        self.open.assert_called_once()
        self.assertIn("ocr_forced", doc.pages[0].flags)
        self.assertEqual(doc.text, HOSTED + "\n")
        doc.to_dict()

    def test_dry_run_counts_pdf_and_tiff_pages_without_key_or_ocr(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            for suffix, body in [
                ("tiff", raster(3)),
                ("pdf", make_pdf([PdfPage(lines=lines_block(["עברית"])) for _ in range(3)])),
            ]:
                for cap, expected in [("0.6", "0.6"), ("0.1", "0"), ("5", "1.089536")]:
                    with self.subTest(suffix=suffix, cap=cap), redirect_stdout(io.StringIO()) as output:
                        path = Path(directory) / f"document.{suffix}"
                        path.write_bytes(body)
                        self.assertEqual(main([
                            str(path), "--ocr-hybrid", "haiku", "--ocr-hybrid-dry-run",
                            "--ocr-hybrid-max-usd", cap, "--max-pages", "2",
                        ]), 0)
                        info = json.loads(output.getvalue())
                        self.assertEqual((info["possible_pages"], info["estimated_max_usd"]), (2, expected))
        self.build_opener.assert_not_called()

    def test_cli_notices_even_in_quiet_mode_and_forwards_consent(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as out:
            path = Path(directory) / "page.png"
            path.write_bytes(self.body)
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main([str(path), "--ocr-hybrid", "haiku", "--no-repairs", "-q"]), 0)
            self.assertIn("Page images may be sent to Anthropic", err.getvalue())
            self.assertEqual(out.getvalue(), HOSTED + "\n")
            self.open.assert_called_once()

    def test_invalid_arguments_cannot_send_images(self):
        for kwargs in (
            {"ocr_hybrid": "other"},
            {"ocr_hybrid_old_print": True},
            {"ocr_hybrid_max_usd": "1"},
            {"ocr_hybrid": "haiku", "ocr": False},
            *({"ocr_hybrid": "haiku", "ocr_hybrid_max_usd": cap} for cap in ("NaN", "inf", "-1", "text")),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.extract(**kwargs)
        self.build_opener.assert_not_called()
