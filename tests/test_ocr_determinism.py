"""The same page, read twice by the real Tesseract while the machine is busy, reads the same.

Tesseract can run its arithmetic on several threads (OpenMP, or Apple's Accelerate), split by the
machine's load; heocr runs every call on one thread as a precaution (``heocr.ocr.tesseract.THREAD_ENV``).
Runs whenever Tesseract with ``heb`` is installed.

The page is invented grainy paper: Tesseract reads bare paper texture as many unsure characters, the
reading most sensitive to a change in the arithmetic. It is a guard, not the evidence: where it was
written, this page also read the same with the one-thread setting switched off. Whether heocr's
output depends on load is checked on real documents, read many times under load, before a release.
"""

import multiprocessing
import os
import time
import unittest

from tests.test_ocr_tables import grainy_paper, px, tesseract_with_heb


def _spin(stop) -> None:
    while not stop.is_set():
        sum(i * i for i in range(10_000))


def _reading(result) -> list:
    return [(line.text, line.bbox, line.confidences, line.paragraph) for line in result.lines]


@unittest.skipUnless(tesseract_with_heb(), "needs Tesseract with heb installed")
class OcrUnderLoadTest(unittest.TestCase):
    def test_a_page_reads_the_same_under_parallel_load(self) -> None:
        from PIL import Image

        from heocr.ocr.tesseract import Tesseract

        engine = Tesseract.find()
        page = grainy_paper().resize((px(595) * 2, px(842) * 2), Image.BILINEAR)
        quiet = _reading(engine.recognize(page, dpi=300))
        self.assertTrue(quiet, "the grainy page gave Tesseract nothing to read")
        stop = multiprocessing.Event()
        busy = [multiprocessing.Process(target=_spin, args=(stop,), daemon=True) for _ in range(os.cpu_count() or 4)]
        for process in busy:
            process.start()
        try:
            time.sleep(0.5)
            loaded = [_reading(engine.recognize(page, dpi=300)) for _ in range(2)]
        finally:
            stop.set()
            for process in busy:
                process.join(timeout=5)
        self.assertEqual(loaded[0], quiet)
        self.assertEqual(loaded[1], quiet)


if __name__ == "__main__":
    unittest.main()
