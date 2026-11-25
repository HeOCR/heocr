"""Perspective-corrected phone OCR on synthetic geometry, with local OCR stubbed."""

from io import BytesIO
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from heocr import extract
from heocr.cli import _parser
from heocr.ocr.phone_geometry import _analysis_raster, _edge_samples, _sheet, read_lines
from heocr.ocr.tesseract import OcrLine, OcrResult, OcrUnavailable, OcrWord
from heocr.pdf.perspective import _projection, _source_quad


def phone_page():
    raster = Image.new("L", (800, 1000), 95)
    quad = ((165, 95), (635, 108), (787, 921), (13, 899))
    draw = ImageDraw.Draw(raster)
    draw.polygon(quad, fill=229)
    transform = _projection(quad, 630, 860, 0)
    lines = []
    for number, y in enumerate(range(160, 590, 47)):
        words = []
        for x in range(73, 552, 51):
            corners = _source_quad(transform, (x, y, x + 35, y + 15))
            draw.polygon(corners, fill=105)
            xs, ys = zip(*corners)
            words.append(OcrWord("בדיקה", [min(xs), min(ys), max(xs), max(ys)], 73))
        if number != 3:
            bounds = [min(w.bbox[0] for w in words), min(w.bbox[1] for w in words),
                      max(w.bbox[2] for w in words), max(w.bbox[3] for w in words)]
            lines.append(OcrLine("בדיקה", bounds, [73] * len(words), words=words))
    return raster, OcrResult(lines)


class PhoneGeometryTest(unittest.TestCase):
    def test_analysis_resize_matches_thumbnail_pixels(self):
        for size in ((203, 234), (1001, 2047), (3072, 1777), (1442, 1442), (2048, 1)):
            with self.subTest(size=size):
                raster = Image.linear_gradient("L").resize(size)
                thumbnail = raster.copy()
                thumbnail.thumbnail((480, 480), Image.Resampling.BOX)
                analysis = _analysis_raster(raster)
                self.assertEqual(analysis.size, thumbnail.size)
                self.assertEqual(analysis.tobytes(), thumbnail.tobytes())
                self.assertEqual(raster.size, size)

    def test_edge_samples_keep_the_innermost_equal_step(self):
        raster = Image.new("L", (140, 120), 40)
        raster.paste(200, (20, 0, 140, 120))
        self.assertEqual(_edge_samples(raster), (0, 23))
        flipped = raster.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        self.assertEqual(_edge_samples(flipped, reverse=True), (0, 116))
        self.assertIsNone(_edge_samples(flipped))

    def test_sheet_detection_rejects_flat_or_incomplete_edges(self):
        raster, _ = phone_page()
        self.assertIsNotNone(_sheet(raster))
        self.assertIsNone(_sheet(raster.crop((180, 90, 620, 900))))
        self.assertIsNone(_sheet(Image.new("L", (800, 1000), 230)))

    def test_line_crops_recover_a_missing_band_and_preserve_numbers(self):
        raster, native = phone_page()
        native.lines[1].text += " 4,553"
        old_box = native.lines[4].bbox

        def mock_batch(images, dpi):
            crops = list(images)
            self.assertEqual((len(crops), dpi), (len(native.lines) + 1, 300))
            for crop in crops:
                self.assertLess(crop.height, raster.height / 8)
                self.assertEqual(crop.getpixel((0, 0)), 255)
            readings = []
            for crop in crops:
                box = [16, 14, crop.width - 16, crop.height - 14]
                readings.append(OcrResult([OcrLine("קריאת בדיקה", box, [93],
                                                    words=[OcrWord("קריאת בדיקה", box, 93)])]))
            return readings

        engine = SimpleNamespace(psm=3, recognize_line_batch=mock_batch)
        result = read_lines(engine, raster, native)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.lines), len(native.lines) + 1)
        self.assertIs(result.lines[1], native.lines[1])
        new_box = result.lines[5].bbox
        self.assertLess(abs(new_box[1] - old_box[1]), 12)
        self.assertLess(abs(new_box[3] - old_box[3]), 12)

    def test_pinned_lstm_skips_a_band_with_an_existing_number_reading(self):
        from heocr.models import MODELS

        raster, native = phone_page()
        native.lines[1].number_readings.append({"first": "4", "second": "4"})
        lengths = []

        def batch(images, dpi):
            crops = list(images)
            lengths.append(len(crops))
            return [OcrResult([OcrLine("בדיקה", [16, 14, 45, 22], [91],
                                       words=[OcrWord("בדיקה", [16, 14, 45, 22], 91)])])
                    for _ in crops]

        engine = SimpleNamespace(psm=3, recognize_line_batch=batch)
        old = read_lines(engine, raster, native)
        engine.version = "tesseract 5.5.3"
        engine.models = {"heocr_modern_v1": MODELS["heocr_modern_v1"].sha256}
        self.assertEqual(read_lines(engine, raster, native), old)
        self.assertEqual(lengths, [len(native.lines) + 1, len(native.lines)])

        native.lines[0].confidences = [99] * len(native.lines[0].words)
        read_lines(engine, raster, native, skip_confident=True)
        self.assertEqual(lengths[-1], len(native.lines) - 1)

    def test_fast_profile_skips_auxiliary_page_reads_on_phone_only(self):
        raster, native = phone_page()
        payload = BytesIO()
        raster.save(payload, format="PNG")
        engine = Mock(psm=3, lang="heb", route=None, version="tesseract 5.5.3")
        engine.recognize.return_value = native
        with (
            patch("heocr.pdf.reader.engine_for", return_value=engine),
            patch("heocr.pdf.reader.number_engine_for", return_value=object()),
            patch("heocr.pdf.reader._reread_number_words", return_value=(0, 0)) as target,
            patch("heocr.pdf.reader._page_number_pass", return_value=((0, 0), None)) as whole_page,
            patch("heocr.ocr.layout.clip_guard", return_value=None) as clip,
            patch("heocr.ocr.phone_geometry.read_lines", return_value=None) as rectify,
        ):
            fast = extract(payload.getvalue(), profile="phone-specialist-fast", ocr_capture_type="pixel")
            self.assertEqual(fast.options["ocr"]["profile"]["name"], "phone-specialist-fast")
            target.assert_called_once()
            whole_page.assert_not_called()
            clip.assert_not_called()
            self.assertTrue(rectify.call_args.kwargs["skip_confident"])

            extract(payload.getvalue(), profile="phone-specialist", ocr_capture_type="pixel")
            whole_page.assert_called_once()
            clip.assert_called_once()
            self.assertNotIn("skip_confident", rectify.call_args.kwargs)

            args = {"ocr_capture_type": "scanner", "ocr_layout": "v1"}
            scan = extract(payload.getvalue(), **args)
            scanned_with_fast = extract(payload.getvalue(), profile="phone-specialist-fast", **args)
            self.assertEqual(scanned_with_fast.to_json(), scan.to_json())

        self.assertEqual(_parser().parse_args(["photo.jpg", "--profile", "phone-specialist-fast"]).profile,
                         "phone-specialist-fast")

    def test_phone_number_batching_respects_capture_opt_in_and_deadline(self):
        raster, native = phone_page()
        payload = BytesIO()
        raster.save(payload, format="PNG")
        engine = Mock(psm=3, lang="heb", route=None, version="tesseract 5.5.3")
        engine.recognize.return_value = native
        with (
            patch("heocr.pdf.reader.engine_for", return_value=engine),
            patch("heocr.pdf.reader.number_engine_for", return_value=object()),
            patch("heocr.pdf.reader._reread_number_words", return_value=(0, 0)) as reread,
            patch("heocr.pdf.reader._page_number_pass", return_value=((0, 0), None)),
            patch("heocr.ocr.phone_geometry.read_lines", return_value=None),
        ):
            for capture, enabled, deadline, batch in (
                ("pixel", True, None, True), ("pixel", False, None, False),
                ("scanner", True, None, False), ("pixel", True, 60, False),
            ):
                with self.subTest(capture=capture, enabled=enabled, deadline=deadline):
                    extract(payload.getvalue(), ocr_capture_type=capture, ocr_phone_geometry=enabled,
                            time_limit=deadline)
                    self.assertEqual(reread.call_args.kwargs["batch"], batch)

    def test_batch_failure_leaves_native_result_available(self):
        raster, native = phone_page()
        batch = Mock(side_effect=OcrUnavailable("local failure"))
        self.assertIsNone(read_lines(SimpleNamespace(psm=3, recognize_line_batch=batch), raster, native))
        batch.assert_called_once()

    def test_alternate_line_reader_is_lazy_and_falls_back_on_an_incomplete_batch(self):
        raster, native = phone_page()
        batch = Mock(return_value=[])
        original = Mock(return_value=[])
        base = SimpleNamespace(psm=3, recognize_line_batch=original)
        alternate = SimpleNamespace(recognize_line_batch=batch, failures=0)
        choose = Mock(return_value=alternate)
        self.assertIsNone(read_lines(base, Image.new("L", raster.size, 255), native, alternate_engine=choose))
        choose.assert_not_called()
        self.assertIsNone(read_lines(base, raster, native, alternate_engine=choose))
        choose.assert_called_once()
        self.assertEqual(alternate.failures, 1)
        original.assert_called_once()

    def test_normal_image_entry_point_gates_by_flag_and_capture(self):
        raster, native = phone_page()
        payload = BytesIO()
        raster.save(payload, format="PNG")
        engine = Mock(psm=3, lang="heb", route=None, version="tesseract 5.5.3")
        engine.recognize.return_value = native
        with (
            patch("heocr.pdf.reader.engine_for", return_value=engine),
            patch("heocr.ocr.phone_geometry.read_lines", return_value=native) as geometry,
        ):
            for capture in ("scanner", "issuer-render", None):
                with self.subTest(capture=capture):
                    options = dict(ocr_capture_type=capture, check_numbers=False)
                    a = extract(payload.getvalue(), **options)
                    b = extract(payload.getvalue(), **options, ocr_phone_geometry=True)
                    self.assertEqual(a.to_json(), b.to_json())
            geometry.assert_not_called()
            extract(payload.getvalue(), ocr_capture_type="pixel", check_numbers=False)
            geometry.assert_not_called()
            doc = extract(payload.getvalue(), ocr_capture_type="pixel", check_numbers=False,
                          ocr_phone_geometry=True)
            geometry.assert_called_once()
            self.assertIn("ocr_phone_geometry_rectified", doc.pages[0].flags)

    def test_fast_mix_uses_the_fast_phone_path_and_working_limit(self):
        raster, _ = phone_page()
        payload = BytesIO()
        raster.resize((1800, 2000)).save(payload, format="PNG")
        with patch("heocr.pdf.reader._ocr_page") as read:
            doc = extract(payload.getvalue(), profile="fast-mix-v1", ocr_capture_type="pixel")
            self.assertEqual(doc.options["ocr"]["profile"]["name"], "fast-mix-v1")
            self.assertEqual(doc.options["check_numbers"], "suspect")
            self.assertTrue(doc._ocr_settings["phone_fast"])
            self.assertTrue(doc._ocr_settings["phone_geometry"])
            self.assertEqual(max(read.call_args.kwargs["rendered"][0].size), 1700)
        self.assertEqual(_parser().parse_args(["page.jpg", "--profile", "fast-mix-v1"]).profile, "fast-mix-v1")

    def test_fast_mix_v2_settings_follow_the_capture(self):
        image, _ = phone_page()
        payload = BytesIO()
        image.save(payload, format="PNG")
        with patch("heocr.pdf.reader._ocr_page"):
            for capture in ("pixel", "issuer-render", "scanner"):
                with self.subTest(capture=capture):
                    doc = extract(payload.getvalue(), profile="fast-mix-v2", ocr_capture_type=capture)
                    self.assertEqual(doc.options["ocr"]["profile"]["name"], "fast-mix-v2")
                    self.assertEqual(bool(doc._ocr_settings.get("phone_latin_edges")), capture == "pixel")
                    self.assertEqual(bool(doc._ocr_settings.get("oblique_rivok")), capture == "pixel")
                    self.assertEqual(bool(doc._ocr_settings.get("issuer_staggered_lanes")), capture == "issuer-render")
        args = _parser().parse_args(["page.jpg", "--profile", "fast-mix-v2"])
        self.assertEqual(args.profile, "fast-mix-v2")

    def test_fast_mix_v3_keeps_the_v2_phone_route(self):
        image, _ = phone_page()
        payload = BytesIO()
        image.save(payload, format="PNG")
        with patch("heocr.pdf.reader._ocr_page"):
            before = extract(payload.getvalue(), profile="fast-mix-v2", ocr_capture_type="pixel")
            after = extract(payload.getvalue(), profile="fast-mix-v3", ocr_capture_type="pixel")
        self.assertEqual(after._ocr_settings, before._ocr_settings)
        self.assertEqual(after.options["ocr"]["profile"]["name"], "fast-mix-v3")
        self.assertIsNone(after._table_proposer)
        self.assertEqual(_parser().parse_args(["page.jpg", "--profile", "fast-mix-v3"]).profile, "fast-mix-v3")

    def test_phone_raster_limit_is_opt_in_and_keeps_source_page_size(self):
        raster, _ = phone_page()
        payload = BytesIO()
        raster.save(payload, format="PNG", dpi=(300, 300))
        edge = max(raster.size) // 2
        with patch("heocr.pdf.reader._ocr_page") as read:
            ordinary = extract(payload.getvalue(), ocr_capture_type="pixel", check_numbers=False)
            source_args = read.call_args
            limited = extract(payload.getvalue(), ocr_capture_type="pixel", check_numbers=False,
                              ocr_phone_max_edge=edge)
            limited_args = read.call_args
            self.assertEqual(source_args.args[3:5], limited_args.args[3:5])
            self.assertEqual(source_args.kwargs["rendered"][0].size, raster.size)
            frame, scale = limited_args.kwargs["rendered"]
            self.assertEqual(max(frame.size), edge)
            self.assertAlmostEqual(scale, source_args.kwargs["rendered"][1] * edge / max(raster.size))
            self.assertIn("ocr_phone_raster_downscaled", limited_args.args[8])
            self.assertEqual(limited_args.args[6]["ocr_pixels"], list(frame.size))
            self.assertEqual(limited.options["ocr"]["phone_max_edge"], edge)
            self.assertNotIn("phone_max_edge", ordinary.options["ocr"])

            scanned = extract(payload.getvalue(), ocr_capture_type="scanner", check_numbers=False,
                              ocr_phone_max_edge=edge)
            self.assertEqual(read.call_args.kwargs["rendered"][0].size, raster.size)
            self.assertNotIn("phone_max_edge", scanned.options["ocr"])

        self.assertEqual(_parser().parse_args(["photo.jpg", "--ocr-phone-max-edge", "1700"]).ocr_phone_max_edge, 1700)
        for invalid in (0, -1, True, 1700.0):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "positive integer"):
                extract(payload.getvalue(), ocr_phone_max_edge=invalid)

    def test_cli_exposes_explicit_opt_in(self):
        parser = _parser()
        self.assertFalse(parser.parse_args(["page.jpg"]).ocr_phone_geometry)
        self.assertTrue(parser.parse_args(["page.jpg", "--ocr-phone-geometry"]).ocr_phone_geometry)

    def test_profile_classifies_camera_jpeg_and_matches_explicit_geometry(self):
        from PIL import ExifTags
        from PIL.TiffImagePlugin import IFDRational

        from heocr.models import MODELS

        raster, native = phone_page()
        exif = Image.Exif()
        exif[ExifTags.IFD.Exif] = {ExifTags.Base.FocalLength: IFDRational(5, 1)}
        payload = BytesIO()
        raster.save(payload, format="JPEG", exif=exif)
        engine = Mock(psm=3, lang="heb", route=None, version="tesseract 5.5.3")
        engine.recognize.return_value = native
        with (
            patch("heocr.pdf.reader.engine_for", return_value=engine),
            patch("heocr.ocr.phone_geometry.read_lines", return_value=native) as geometry,
        ):
            current = extract(payload.getvalue(), check_numbers=False, profile="phone-specialist").to_dict()
            existing = extract(payload.getvalue(), check_numbers=False, ocr_router="v2", ocr_layout="v1",
                               ocr_phone_geometry=True).to_dict()
            self.assertEqual(geometry.call_count, 2)
            self.assertEqual(current["options"]["ocr"]["capture_type"], "camera-photo")
            self.assertEqual(current["options"]["ocr"]["router"]["reason"], "camera_exif")
            sha = MODELS["heocr_modern_v1"].sha256
            self.assertEqual(current["options"]["ocr"].pop("profile"), {
                "name": "phone-specialist", "line_model_sha256": sha,
            })
            self.assertEqual(current["pages"][0]["signals"].pop("ocr_phone_geometry_line_sha256"), sha)
            self.assertEqual(current, existing)

    def test_profile_bypasses_other_captures_and_does_not_open_the_slot(self):
        from tests.synth.pdf import PdfPage, lines_block, make_pdf

        raster, native = phone_page()
        payload = BytesIO()
        raster.save(payload, format="PNG")
        pdf = make_pdf([PdfPage(lines=lines_block(["בדיקת עמוד"]))])
        engine = Mock(psm=3, lang="heb", route=None, version="tesseract 5.5.3")
        engine.recognize.return_value = native
        with (
            patch("heocr.pdf.reader.engine_for", return_value=engine),
            patch("heocr.ocr.phone_geometry.read_lines") as geometry,
            patch("heocr.ocr.phone_profile.check_model") as check,
        ):
            for body, capture in ((payload.getvalue(), "scanner"), (payload.getvalue(), "issuer-render"),
                                  (payload.getvalue(), None), (pdf, None), (pdf, "pixel")):
                for router in (None, "v2"):
                    with self.subTest(capture=capture, router=router, pdf=body == pdf):
                        options = dict(ocr_capture_type=capture, ocr_router=router, check_numbers=False)
                        plain = extract(body, **options)
                        profiled = extract(body, **options, profile="phone-specialist",
                                           phone_model="missing.traineddata", phone_model_sha256="a" * 64)
                        self.assertEqual(plain.to_json(), profiled.to_json())
            geometry.assert_not_called()
            check.assert_not_called()

    def test_phone_model_pin_validation_and_failed_custom_batches(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        raster, native = phone_page()
        payload = BytesIO()
        raster.save(payload, format="PNG")
        body = payload.getvalue()
        with TemporaryDirectory() as directory:
            model = Path(directory) / "candidate.traineddata"
            model.write_bytes(b"fixture model")
            with self.assertRaisesRegex(ValueError, "phone model is unavailable or does not match"):
                extract(body, profile="phone-specialist", ocr_capture_type="phone",
                        phone_model=model, phone_model_sha256="a" * 64)
            with self.assertRaisesRegex(ValueError, "supplied together"):
                extract(body, profile="phone-specialist", phone_model=model)
            with self.assertRaisesRegex(ValueError, "supplied together"):
                extract(body, phone_model=model, phone_model_sha256="a" * 64)
            with self.assertRaisesRegex(ValueError, "64-digit SHA-256"):
                extract(body, profile="phone-specialist", phone_model=model, phone_model_sha256="bad")
        custom = SimpleNamespace(recognize_line_batch=Mock(side_effect=OcrUnavailable("local failure")))
        with self.assertRaisesRegex(ValueError, "phone specialist line OCR failed"):
            read_lines(SimpleNamespace(psm=3), raster, native, line_engine=lambda: custom)

    def test_profile_cli_arguments(self):
        args = _parser().parse_args(["page.jpg", "--profile", "phone-specialist",
                                     "--phone-model", "specialist.traineddata",
                                     "--phone-model-sha256", "9" * 64])
        self.assertEqual(args.profile, "phone-specialist")
        self.assertEqual(args.phone_model.name, "specialist.traineddata")
        self.assertEqual(args.phone_model_sha256, "9" * 64)
