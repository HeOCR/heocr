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
from copy import deepcopy
from pathlib import Path
from threading import Lock

PINNED = {
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors":
        "2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9",
    "docling-project--docling-layout-heron/model.safetensors":
        "00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c",
}
REVISIONS = {"docling-project/docling-models": "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
             "docling-project/docling-layout-heron": "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"}
INSTALL_HINT = "the TableFormer proposer needs the optional extra: pip install 'heocr[tables]'"


def verify_models(models: str | Path) -> dict[str, str]:
    """sha256 of each pinned weight file under ``models``; raises when one is missing or differs."""
    root = Path(models)
    found = {}
    for rel, expected in PINNED.items():
        path = root / rel
        if not path.is_file():
            raise FileNotFoundError(f"missing pinned table model file {rel} under the tables_models directory")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise ValueError(f"{rel}: sha256 {digest[:12]}… differs from the pinned {expected[:12]}…")
        found[rel] = digest
    return found


class TableFormerSession:
    """Reuse pinned, local Docling models across documents in one process.

    Keep the local model directory fixed for the session. Conversion is serial
    even if callers share the session across threads.
    """

    def __init__(self, models: str | Path):
        try:
            import docling  # noqa: F401
            from importlib.metadata import version
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError(INSTALL_HINT) from exc
        self._models = Path(models).resolve()
        weights = verify_models(self._models)
        self._record = {"proposer": "tableformer", "docling": version("docling"),
                        "docling_ibm_models": version("docling-ibm-models"), "revisions": dict(REVISIONS),
                        "weights_sha256": {Path(k).name: v for k, v in weights.items()}}
        self._converter = None
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


class TableFormerProposer:
    """Document-scoped proposal cache. Only the model runtime can be shared."""

    def __init__(self, models: str | Path | TableFormerSession):
        self._session = models if isinstance(models, TableFormerSession) else TableFormerSession(models)
        self.record = deepcopy(self._session._record)
        self._key: str | None = None
        self._pages: dict[int, list[dict]] = {}

    def _convert(self, body: bytes) -> None:
        doc = self._session._convert(body)
        pages: dict[int, list[dict]] = {}
        for table in doc.tables:
            if not table.prov:
                continue
            page_no = table.prov[0].page_no
            height = doc.pages[page_no].size.height
            box = table.prov[0].bbox.to_top_left_origin(height)
            cells = []
            for cell in table.data.table_cells:
                if cell.bbox is None:
                    continue
                b = cell.bbox.to_top_left_origin(height)
                cells.append({"row": cell.start_row_offset_idx, "col": cell.start_col_offset_idx,
                              "rowspan": max(1, cell.end_row_offset_idx - cell.start_row_offset_idx),
                              "colspan": max(1, cell.end_col_offset_idx - cell.start_col_offset_idx),
                              "box": [b.l, b.t, b.r, b.b]})
            pages.setdefault(page_no - 1, []).append(
                {"bbox": [box.l, box.t, box.r, box.b], "rows": table.data.num_rows, "cols": table.data.num_cols,
                 "cells": cells, "page_size": [doc.pages[page_no].size.width, height]})
        self._pages = pages

    def page_tables(self, body: bytes, index: int, width: float, height: float) -> list[dict]:
        """Proposals for page ``index`` (0-based) in its top-left frame; none when Docling's page size differs."""
        key = hashlib.sha256(body).hexdigest()
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
