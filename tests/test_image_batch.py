import tempfile
from pathlib import Path
from threading import Barrier
from unittest import TestCase, mock

import heocr
from heocr.api import _extract_image
from tests.test_images import encoded, page_image


class ImageBatchTests(TestCase):
    def test_parallel_records_match_serial_extraction_in_order(self):
        png = encoded(page_image(), "PNG", dpi=(150, 150))
        jpeg = encoded(page_image(ink=False).convert("RGB"), "JPEG")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page.png"
            path.write_bytes(png)
            inputs = [path, jpeg]
            options = {"ocr": False, "repairs": False}
            expected = [heocr.extract(source, **options).to_json(indent=None) for source in inputs]
            overlapping = Barrier(2, timeout=5)

            def read(source, **kwargs):
                overlapping.wait()
                return _extract_image(source, **kwargs)

            with mock.patch("heocr.api._extract_image", side_effect=read):
                records = list(heocr.extract_images(iter(inputs), workers=2, **options))
            self.assertEqual([doc.to_json(indent=None) for doc in records], expected)

    def test_empty_inputs_and_invalid_worker_count(self):
        self.assertEqual(list(heocr.extract_images(iter(()), workers=2)), [])
        for workers in (True, 0, 3, 1.0):
            with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, "workers"):
                list(heocr.extract_images([], workers=workers))

    def test_non_images_are_rejected_before_entering_the_reader(self):
        for workers in (1, 2):
            with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, "standalone image"):
                list(heocr.extract_images([b"%PDF-1.5\n"], workers=workers))
