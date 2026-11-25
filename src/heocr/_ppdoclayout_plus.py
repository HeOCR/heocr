"""Run the pinned Paddle layout predictor in a warm, local Python process."""
from __future__ import annotations

from base64 import b64decode, b64encode
from contextlib import suppress
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from weakref import finalize

MODEL = "PP-DocLayout_plus-L"
VERSIONS = {"paddlepaddle": "3.3.1", "paddleocr": "3.7.0", "paddlex": "3.7.2",
            "numpy": "2.3.5", "opencv-contrib-python": "4.10.0.84"}
THRESHOLD = 0.35
EMIT_THRESHOLD = 0.01


def _stop(process):
    with suppress(OSError):
        process.stdin.close()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    process.stdout.close()


class PpDocLayoutPlus:
    """Send page rasters over local pipes, with no shared Torch/Paddle imports."""

    def __init__(self, models: Path, python: Path):
        try:
            self._process = subprocess.Popen(
                [str(python), "-I", str(Path(__file__).resolve()), str(models)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                env={**os.environ, "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True", "HF_HUB_OFFLINE": "1"},
            )
        except OSError as exc:
            raise RuntimeError("could not start the local PP-DocLayout_plus-L runtime") from exc
        self._cleanup = finalize(self, _stop, self._process)

    def close(self):
        self._cleanup()

    def boxes(self, image):
        png = io.BytesIO()
        image.save(png, format="PNG")
        try:
            self._process.stdin.write(b64encode(png.getbuffer()) + b"\n")
            self._process.stdin.flush()
            response = self._process.stdout.readline()
            return json.loads(response)
        except (OSError, ValueError) as exc:
            self.close()
            raise RuntimeError("the local PP-DocLayout_plus-L worker stopped before returning boxes") from exc


def _table_boxes(predictor, image):
    result, = predictor.predict(image, batch_size=1, threshold=EMIT_THRESHOLD)
    return [[float(v) for v in entry["coordinate"]] for entry in result["boxes"]
            if entry["label"].casefold() == "table" and float(entry["score"]) > THRESHOLD]


def _serve(models: str, response):
    from importlib.metadata import version

    for package, expected in VERSIONS.items():
        if version(package) != expected:
            raise ValueError(f"PP-DocLayout_plus-L needs {package}=={expected}")
    import cv2
    import numpy
    from paddleocr import LayoutDetection

    predictor = LayoutDetection(model_name=MODEL, model_dir=models, device="cpu",
                                enable_mkldnn=False, cpu_threads=4, threshold=EMIT_THRESHOLD)
    try:
        for request in sys.stdin.buffer:
            raster = cv2.imdecode(numpy.frombuffer(b64decode(request), dtype=numpy.uint8), cv2.IMREAD_COLOR)
            if raster is None:
                raise ValueError("cannot decode the local detector raster")
            response.write((json.dumps(_table_boxes(predictor, raster), allow_nan=False) + "\n").encode())
            response.flush()
    finally:
        predictor.close()


if __name__ == "__main__":
    # Native inference libraries can log to stdout. Keep the response pipe separate.
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb") as protocol:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        _serve(sys.argv[1], protocol)
