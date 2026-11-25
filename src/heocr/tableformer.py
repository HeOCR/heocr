"""TableFormer table proposals through Docling (D3g; the optional ``tables`` extra).

Docling (MIT) runs its layout model (docling-layout-heron, Apache-2.0) and TableFormer (docling-models,
CDLA-Permissive-2.0 / Apache-2.0) on a PDF and reports each table's cell structure with boxes. heocr keeps only that
structure: the cells' text comes from heocr's own reading (``pdf/proposed.py``), never from Docling.

**Offline and pinned.** heocr never downloads these weights. The user fetches them on purpose into a directory and
passes it; both weight files are checked against the pinned sha256 before any use, and Docling runs with that
directory only and with OCR off. To fetch the pinned revisions::

    hf download docling-project/docling-models --revision fc0f2d45e2218ea24bce5045f58a389aed16dc23 --local-dir DIR/docling-project--docling-models
    hf download docling-project/docling-layout-heron --revision 8f39ad3c0b4c58e9c2d2c84a38465abf757272d8 --local-dir DIR/docling-project--docling-layout-heron
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from copy import deepcopy
from pathlib import Path
from threading import Lock

STRUCTURE_DIR = "docling-project--docling-models/model_artifacts/tableformer/accurate"
STRUCTURE_WEIGHTS = f"{STRUCTURE_DIR}/tableformer_accurate.safetensors"
STRUCTURE_CONFIG = f"{STRUCTURE_DIR}/tm_config.json"
PINNED = {STRUCTURE_WEIGHTS: "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9",
          "docling-project--docling-layout-heron/model.safetensors":
              "00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c"}
REVISIONS = {"docling-project/docling-models": "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
             "docling-project/docling-layout-heron": "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"}
DETECTOR_NAME = "heron_phone"
DETECTOR_DIR = "heocr--heron-phone"
DETECTOR_PINNED = {
    STRUCTURE_WEIGHTS: PINNED[STRUCTURE_WEIGHTS],
    STRUCTURE_CONFIG: "984e122ceb8ccf84d84c9d2882f6f2302a44b4f1e577babd6289892c36f3cffd",
    f"{DETECTOR_DIR}/model.safetensors": "44a22ed18dc335a88ba11eac706d3707442c3b0114893bf93e6eb331c9cb1e4f",
    f"{DETECTOR_DIR}/config.json": "fdea30805ce2f5666b147fca941dcdd27ad468e27d6ed21902207d3da056a97d",
    f"{DETECTOR_DIR}/preprocessor_config.json": "cd38cd59999e7a95d68e487fbe5132df3d4e5c32a0836add57e6126ba0c4eaf1",
}
DETECTOR_VERSIONS = {"docling": "2.152.0", "docling-ibm-models": "4.0.4", "transformers": "5.24.1"}
INSTALL_HINT = "the TableFormer proposer needs the optional extra: pip install 'heocr[tables]'"


def verify_models(models: str | Path) -> dict[str, str]:
    """sha256 of each pinned weight file under ``models``; raises when one is missing or differs."""
    return _verify_files(Path(models), PINNED)


def _verify_files(root: Path, pins: dict[str, str]) -> dict[str, str]:
    found = {}
    for rel, expected in pins.items():
        path = root / rel
        if not path.is_file():
            raise FileNotFoundError(f"missing pinned table model file {rel} under the tables_models directory")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError(f"{rel}: sha256 {digest[:12]}… differs from the pinned {expected[:12]}…")
        found[rel] = digest
    return found


class TableFormerSession:
    """Reuse pinned, local Docling models across documents in one process.

    Keep the local model directory fixed for the session. Conversion is serial
    even if callers share the session across threads.
    """

    def __init__(self, models: str | Path, *, detector: str | None = None):
        if detector not in (None, DETECTOR_NAME):
            raise ValueError(f"unknown table detector {detector!r}")
        try:
            import docling  # noqa: F401
            from importlib.metadata import version
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError(INSTALL_HINT) from exc
        self._models = Path(models).resolve()
        self.detector = detector
        weights = verify_models(self._models) if detector is None else _verify_files(self._models, DETECTOR_PINNED)
        self._record = {"proposer": "tableformer", "docling": version("docling"),
                        "docling_ibm_models": version("docling-ibm-models"), "revisions": dict(REVISIONS),
                        "weights_sha256": {Path(k).name: v for k, v in weights.items()}}
        if detector is not None:
            for package, expected in DETECTOR_VERSIONS.items():
                if version(package) != expected:
                    raise ValueError(f"table detector {detector} needs {package}=={expected}")
            self._record.update(detector=detector, detector_base_revision=REVISIONS["docling-project/docling-layout-heron"],
                                transformers=DETECTOR_VERSIONS["transformers"],
                                revisions={"docling-project/docling-models": REVISIONS["docling-project/docling-models"]},
                                weights_sha256={Path(k).name: v for k, v in weights.items() if k.endswith(".safetensors")},
                                config_sha256={Path(k).name: v for k, v in weights.items() if k.endswith(".json")})
        self._converter = None
        self._box_detector = None
        self._predictor = None
        self._lock = Lock()

    def _make_converter(self):
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions(do_ocr=False, do_table_structure=True, artifacts_path=str(self._models))
        opts.table_structure_options.mode = TableFormerMode.ACCURATE
        opts.table_structure_options.do_cell_matching = False  # structure only; heocr reads the text
        return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})

    def _convert(self, body: bytes):
        from docling.datamodel.base_models import DocumentStream

        with self._lock:
            if self._converter is None:
                self._converter = self._make_converter()
            try:
                return self._converter.convert(DocumentStream(name="document.pdf", stream=io.BytesIO(body))).document
            except Exception:
                # A failed backend may leave its pipeline in a bad state.
                self._converter = None
                raise

    def _detected_tables(self, body: bytes, index: int, width: float, height: float, on_detection) -> list[dict] | None:
        import pypdfium2

        with self._lock, pypdfium2.PdfDocument(body) as document:
            if not 0 <= index < len(document):
                return None
            page = document[index]
            try:
                size = page.get_size()
                if abs(size[0] - width) > 1 or abs(size[1] - height) > 1:
                    return None
                bitmap = page.render(scale=1)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            if self._box_detector is None:
                self._box_detector = _HeronDetector(self._models / DETECTOR_DIR)
            boxes = self._box_detector(image, size)
            on_detection()
            if not boxes:
                return []
            if self._predictor is None:
                self._predictor = _load_predictor(self._models)
            predictor, max_steps = self._predictor
            return _predict_structure(predictor, max_steps, body, index, size, boxes)


class _HeronDetector:
    """Pinned page-region detector from the phone TRAIN comparison."""

    def __init__(self, path: Path):
        from transformers import AutoImageProcessor, AutoModelForObjectDetection

        self.processor = AutoImageProcessor.from_pretrained(path, local_files_only=True)
        self.model = AutoModelForObjectDetection.from_pretrained(
            path, local_files_only=True, use_safetensors=True
        ).eval()
        self.labels = {i for i, name in self.model.config.id2label.items() if name.lower() in {"table", "document_index"}}

    def __call__(self, image, size: tuple[float, float]) -> list[list[float]]:
        import torch

        width, height = size
        with torch.inference_mode():
            prediction = self.model(**self.processor(images=image, return_tensors="pt"))
            decoded = self.processor.post_process_object_detection(
                prediction, threshold=0.5, target_sizes=torch.tensor([[height, width]])
            )[0]
        boxes = []
        for label, box in zip(decoded["labels"].tolist(), decoded["boxes"].tolist(), strict=True):
            if label not in self.labels or not all(math.isfinite(value) for value in box):
                continue
            left, top, right, bottom = box
            clipped = [max(0, left), max(0, top), min(width, right), min(height, bottom)]
            if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
                boxes.append(clipped)
        return boxes


def _load_predictor(models: Path):
    from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor

    root = models / STRUCTURE_DIR
    # TFPredictor loads the first matching filename. Only the verified artifact may match.
    if set(root.glob("tableformer_*.safetensors")) != {models / STRUCTURE_WEIGHTS}:
        raise ValueError("expected only the pinned TableFormer weights under the accurate model directory")
    config = json.loads((models / STRUCTURE_CONFIG).read_text(encoding="utf-8"))
    config["model"]["save_dir"] = str(root)
    return TFPredictor(config, device="cpu"), config["predict"]["max_steps"]


def _predict_structure(predictor, max_steps, body, index, size, boxes):
    """Use only the PDF backend and TableFormer, bypassing Docling layout inference."""
    import numpy
    from docling.backend.docling_parse_backend import ThreadedDoclingParseDocumentBackend
    from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.document import InputDocument
    from docling.datamodel.settings import DocumentLimits
    from docling.models.base_table_model import degenerate_structure_reason
    from docling_core.types.doc import BoundingBox, CoordOrigin, TableCell
    from docling_core.types.doc.page import TextCellUnit

    scale = 2  # Docling's TableFormer stage renders at 144 dpi.
    with io.BytesIO(body) as stream:
        options = ThreadedDoclingParseBackendOptions(render_pages=True, render_scale=scale)
        limits = DocumentLimits(page_range=(index + 1, index + 1))
        doc = InputDocument(stream, InputFormat.PDF, ThreadedDoclingParseDocumentBackend, options,
                            filename="document.pdf", limits=limits)
        backend = doc._backend
        if backend is None:
            raise ValueError("cannot open PDF for direct TableFormer")
        try:
            pages = backend.iter_pages()
            try:
                page = next(pages, None)
            finally:
                pages.close()
            if page is None:
                raise ValueError("cannot load PDF page for direct TableFormer")
            try:
                if not doc.valid or not page.is_valid():
                    raise ValueError("cannot load PDF page for direct TableFormer")
                frame = page.get_size()
                if abs(frame.width - size[0]) > 1 or abs(frame.height - size[1]) > 1:
                    return []
                parsed = page.get_segmented_page()
                image = numpy.asarray(page.get_page_image(scale=scale))
                proposals = []
                for box in boxes:
                    region = BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin=CoordOrigin.TOPLEFT)
                    cells = [] if parsed is None else parsed.get_cells_in_bbox(TextCellUnit.WORD, region)
                    if not cells and parsed is not None:
                        cells = parsed.get_cells_in_bbox(TextCellUnit.LINE, region)
                    tokens = [{"id": i, "text": cell.text,
                               "bbox": cell.rect.to_bounding_box().to_top_left_origin(size[1]).scaled(scale).model_dump()}
                              for i, cell in enumerate(cells) if cell.text.strip()]
                    inp = {"image": image, "width": image.shape[1], "height": image.shape[0], "tokens": tokens}
                    result, = predictor.multi_table_predict(inp, [[v * scale for v in box]], do_matching=False)
                    details = result["predict_details"]
                    if degenerate_structure_reason(details.get("prediction", {}).get("rs_seq", []), max_steps):
                        continue
                    if not details["num_rows"] or not details["num_cols"]:
                        continue
                    table_cells = [TableCell.model_validate(cell) for cell in result["tf_responses"]]
                    proposals.append(_proposal(box, details["num_rows"], details["num_cols"], table_cells, size, scale))
                return proposals
            finally:
                page.unload()
        finally:
            backend.unload()


def _proposal(box, rows, cols, table_cells, size, scale=1):
    cells = []
    for cell in table_cells:
        if cell.bbox is None:
            continue
        b = cell.bbox.to_top_left_origin(size[1] * scale)
        cells.append({"row": cell.start_row_offset_idx, "col": cell.start_col_offset_idx,
                      "rowspan": max(1, cell.end_row_offset_idx - cell.start_row_offset_idx),
                      "colspan": max(1, cell.end_col_offset_idx - cell.start_col_offset_idx),
                      "box": [v / scale for v in (b.l, b.t, b.r, b.b)]})
    return {"bbox": list(box), "rows": rows, "cols": cols, "cells": cells, "page_size": list(size)}


class TableFormerProposer:
    """Document-scoped proposal cache. Only the model runtime can be shared."""

    def __init__(self, models: str | Path | TableFormerSession, *, detector: str | None = None):
        if isinstance(models, TableFormerSession):
            if detector is not None and models.detector != detector:
                raise ValueError("the table detector must match the shared session")
            self._session = models
        else:
            self._session = TableFormerSession(models, detector=detector)
        self.record = deepcopy(self._session._record)
        self._on_detection = None
        self._key: str | None = None
        self._pages: dict[int | tuple[int, float, float], list[dict]] = {}

    def _convert(self, body: bytes) -> None:
        doc = self._session._convert(body)
        pages: dict[int, list[dict]] = {}
        for table in doc.tables:
            if not table.prov:
                continue
            page_no = table.prov[0].page_no
            size = doc.pages[page_no].size
            box = table.prov[0].bbox.to_top_left_origin(size.height)
            pages.setdefault(page_no - 1, []).append(
                _proposal([box.l, box.t, box.r, box.b], table.data.num_rows, table.data.num_cols,
                          table.data.table_cells, (size.width, size.height)))
        self._pages = pages

    def _detection_used(self):
        if self._on_detection is not None:
            self._on_detection()

    def page_tables(self, body: bytes, index: int, width: float, height: float) -> list[dict]:
        """Proposals for page ``index`` (0-based) in its top-left frame; none when Docling's page size differs."""
        key = hashlib.sha256(body).hexdigest()
        if self._session.detector is not None:
            if key != self._key:
                self._key, self._pages = key, {}
            cache_key = (index, width, height)
            if cache_key in self._pages:
                self._detection_used()
            else:
                proposals = self._session._detected_tables(body, index, width, height, self._detection_used)
                if proposals is None:
                    return []
                self._pages[cache_key] = proposals
            return self._pages[cache_key]
        if key != self._key:
            self._convert(body)
            self._key = key
        out = []
        for p in self._pages.get(index, []):
            pw, ph = p["page_size"]
            if abs(pw - width) > 1.0 or abs(ph - height) > 1.0:
                continue  # a rotated or cropped frame: the boxes would not meet the layer
            out.append(p)
        return out
