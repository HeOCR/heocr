"""The ``heocr_document.v1`` record: what was read, by which route, and what was not.

The JSON record is the source of truth. ``Document.text`` and ``Document.markdown``
are views of its blocks. Records carry no local path and no file name.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_ID = "heocr_document.v1"

BLOCK_KINDS = ("text", "heading", "list_item", "table", "figure")
ROLES = ("body", "header", "footer", "footnote", "endnote", "comment", "textbox")
SOURCES = ("text_layer", "office_xml", "converted_docx", "ocr", "none")
PAGE_CLASSES = ("T1", "T2", "T3", "T4", "T5", "T6", "empty")
DIRECTIONS = ("rtl", "ltr")
UNREAD_SCOPES = ("document", "page", "block")

# Strings that look like local filesystem locations. Provenance must stay path-free;
# document content (block text, cells) is content and is not path-checked.
LOCAL_PATH = re.compile(r"(\A(/|~/|[A-Za-z]:[\\/]|\\\\|file:))|(/(Users|home|tmp|private|var)/)")


class ContractError(ValueError):
    """A record does not satisfy ``heocr_document.v1``."""


@dataclass
class Cell:
    text: str
    colspan: int = 1
    rowspan: int = 1
    flags: list[str] = field(default_factory=list)
    # OCR'd cells whose number was read by two models: each reading, {"model", "text", "confidence"}.
    readings: list[dict[str, Any]] | None = None
    _lines: Any = field(default=None, repr=False, compare=False)  # PDF layer cells: [text, bbox] per line, for number checks

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text}
        if self.colspan != 1:
            out["colspan"] = self.colspan
        if self.rowspan != 1:
            out["rowspan"] = self.rowspan
        if self.flags:
            out["flags"] = list(self.flags)
        if self.readings:
            out["readings"] = [dict(r) for r in self.readings]
        return out


@dataclass
class Line:
    """One physical line of a block, as read from a paged source."""

    text: str
    bbox: list[float]
    direction: str = "rtl"

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "bbox": _round_box(self.bbox), "direction": self.direction}


@dataclass
class Block:
    kind: str
    text: str = ""
    role: str = "body"
    source: str = "none"
    tool: str = ""
    page: int | None = None
    bbox: list[float] | None = None
    level: int | None = None
    label: str | None = None
    note_id: str | None = None
    direction: str | None = None
    cells: list[list[Cell]] | None = None
    lines: list[Line] | None = None
    ocr: dict[str, Any] | None = None  # for source "ocr": engine, settings, status "unverified", confidence
    note_refs: list[dict[str, Any]] | None = None  # footnote/endnote references: kind, id, offset in text
    checks: dict[str, Any] | None = None  # second readings: {"numbers": checked, "agreed": agreed}
    repairs: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    half: str | None = None  # a PDF spread split in two (D4c): "right" or "left", the page it belongs to
    _cell_repairs: Any = field(default=None, repr=False, compare=False)  # PDF tables: repairs made in cells, logged by the reader

    def to_dict(self, index: int) -> dict[str, Any]:
        out: dict[str, Any] = {"id": f"b{index}", "kind": self.kind, "role": self.role}
        if self.kind == "table":
            out["cells"] = [[cell.to_dict() for cell in row] for row in (self.cells or [])]
        else:
            out["text"] = self.text
        for key in ("level", "label", "note_id", "direction", "page", "half"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.bbox is not None:
            out["bbox"] = _round_box(self.bbox)
        if self.lines is not None:
            out["lines"] = [line.to_dict() for line in self.lines]
        if self.ocr is not None:
            out["ocr"] = dict(self.ocr)
        if self.note_refs:
            out["note_refs"] = [dict(r) for r in self.note_refs]
        if self.checks:
            out["checks"] = dict(self.checks)
        out["source"] = self.source
        out["tool"] = self.tool
        out["repairs"] = list(self.repairs)
        out["flags"] = sorted(set(self.flags))
        return out


@dataclass
class Page:
    index: int
    width: float
    height: float
    page_class: str
    route: str
    signals: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "class": self.page_class,
            "route": self.route,
            "signals": dict(self.signals),
            "coverage": dict(self.coverage),
            "flags": sorted(set(self.flags)),
        }


# Pages (PDF pages, image frames) read at most per document. Every page may cost an OCR run of
# seconds, so a document of tens of thousands of pages, hostile or merely huge, would run for
# hours. The rest are recorded as unread, in one entry, never dropped silently.
MAX_PAGES = 2000


def page_limit(doc: "Document", total: int) -> int:
    """The number of pages to read; records the rest as one unread entry and flags the document."""
    limit = doc._max_pages
    if total <= limit:
        return total
    doc.unread.append(Unread(scope="document", reason="page_limit", detail=f"pages {limit + 1}-{total} of {total} not read (max_pages={limit})"))
    doc.flags.append("page_limit_reached")
    return limit


@dataclass
class Unread:
    """Something the pipeline saw but did not read, and why. Never dropped silently."""

    scope: str
    reason: str
    page: int | None = None
    detail: str | None = None
    bbox: list[float] | None = None  # the unread region, in the page's coordinates

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"scope": self.scope, "reason": self.reason}
        if self.page is not None:
            out["page"] = self.page
        if self.bbox is not None:
            out["bbox"] = _round_box(self.bbox)
        if self.detail is not None:
            out["detail"] = self.detail
        return out


@dataclass
class Document:
    sha256: str
    size: int
    format: str
    format_evidence: str
    page_count: int | None = None
    pages: list[Page] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    unread: list[Unread] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    components: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=lambda: {"repairs": True})
    # Run-time only, never serialized: OCR settings and the engine found for this document.
    _ocr_settings: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    _ocr_engine: Any = field(default=None, repr=False, compare=False)
    _ocr_error: str | None = field(default=None, repr=False, compare=False)
    _convert_settings: dict[str, Any] | None = field(default=None, repr=False, compare=False)  # LibreOffice: cmd, timeout
    _max_pages: int = field(default=MAX_PAGES, repr=False, compare=False)  # pages read at most
    _number_engine: Any = field(default=None, repr=False, compare=False)  # the second reading of numbers (eng)
    _number_error: str | None = field(default=None, repr=False, compare=False)
    _font_codes: dict = field(default_factory=dict, repr=False, compare=False)  # (font, letter) -> code
    _rtl: bool = field(default=True, repr=False, compare=False)  # the document's direction (PDF tables)
    # font_private_use_by_ocr: the maps learned for this document, with their evidence (no text).
    font_maps: list[dict[str, Any]] = field(default_factory=list)

    # -- views -------------------------------------------------------------
    @property
    def text(self) -> str:
        from heocr.render import to_text

        return to_text(self)

    @property
    def markdown(self) -> str:
        from heocr.render import to_markdown

        return to_markdown(self)

    # -- record ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        from heocr import __version__

        document: dict[str, Any] = {
            "sha256": self.sha256,
            "bytes": self.size,
            "format": self.format,
            "format_evidence": self.format_evidence,
        }
        if self.page_count is not None:
            document["page_count"] = self.page_count
        payload = {
            "schema": SCHEMA_ID,
            "producer": {
                "name": "heocr",
                "version": __version__,
                "components": dict(sorted(self.components.items())),
            },
            "document": document,
            "options": dict(self.options),
            "pages": [page.to_dict() for page in self.pages],
            "blocks": [block.to_dict(i) for i, block in enumerate(self.blocks)],
            "unread": [item.to_dict() for item in self.unread],
            "flags": sorted(set(self.flags)),
        }
        if self.font_maps:
            payload["font_maps"] = [dict(m) for m in self.font_maps]
        errors = validate(payload)
        if errors:
            raise ContractError("; ".join(errors))
        return payload

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _round_box(box: list[float]) -> list[float]:
    return [round(float(v), 2) for v in box]


# -- validation ----------------------------------------------------------------


def _path_like(value: Any, where: str):
    if isinstance(value, str):
        if LOCAL_PATH.search(value):
            yield where
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _path_like(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _path_like(item, f"{where}[{index}]")


def _is_box(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value)
        and value[2] >= value[0]
        and value[3] >= value[1]
    )


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def validate(payload: Any) -> list[str]:
    """Return the list of contract violations in a ``heocr_document.v1`` payload."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["record is not an object"]
    if payload.get("schema") != SCHEMA_ID:
        errors.append(f"schema must be {SCHEMA_ID!r}")
    producer = payload.get("producer")
    if not isinstance(producer, dict) or producer.get("name") != "heocr" or not isinstance(
        producer.get("version"), str
    ):
        errors.append("producer must name heocr and its version")
    document = payload.get("document")
    if not isinstance(document, dict):
        errors.append("document must be an object")
        document = {}
    sha = document.get("sha256")
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        errors.append("document.sha256 must be 64 lowercase hex digits")
    if not isinstance(document.get("bytes"), int) or document.get("bytes", -1) < 0:
        errors.append("document.bytes must be a non-negative integer")
    for key in ("format", "format_evidence"):
        if not isinstance(document.get(key), str) or not document.get(key):
            errors.append(f"document.{key} must be a non-empty string")
    for key in ("name", "path", "filename", "file"):
        if key in document:
            errors.append(f"document.{key} is not allowed: records carry no file names or paths")

    options = payload.get("options", {})
    if not isinstance(options, dict) or not isinstance(options.get("repairs", True), bool):
        errors.append("options must be an object, with a boolean repairs")
    elif "ocr" in options and not (options["ocr"] is False or isinstance(options["ocr"], dict)):
        errors.append("options.ocr must be false or an object")
    elif "max_pages" in options and not (isinstance(options["max_pages"], int) and not isinstance(options["max_pages"], bool) and options["max_pages"] > 0):
        errors.append("options.max_pages must be a positive integer")
    elif "convert" in options and not (options["convert"] is False or isinstance(options["convert"], dict)):
        errors.append("options.convert must be false or an object")
    elif options.get("check_numbers", False) not in (False, "suspect", "tables", "all"):
        errors.append("options.check_numbers must be 'suspect', 'tables', 'all' or false")

    pages = payload.get("pages")
    if not isinstance(pages, list):
        errors.append("pages must be a list")
        pages = []
    for i, page in enumerate(pages):
        where = f"pages[{i}]"
        if not isinstance(page, dict):
            errors.append(f"{where} is not an object")
            continue
        if page.get("index") != i:
            errors.append(f"{where}.index must be {i}")
        if page.get("class") not in PAGE_CLASSES:
            errors.append(f"{where}.class must be one of {PAGE_CLASSES}")
        if not isinstance(page.get("route"), str):
            errors.append(f"{where}.route must be a string")
        for key in ("signals", "coverage"):
            if not isinstance(page.get(key), dict):
                errors.append(f"{where}.{key} must be an object")
        if not _strings(page.get("flags")):
            errors.append(f"{where}.flags must be a list of strings")

    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        errors.append("blocks must be a list")
        blocks = []
    for i, block in enumerate(blocks):
        where = f"blocks[{i}]"
        if not isinstance(block, dict):
            errors.append(f"{where} is not an object")
            continue
        if block.get("id") != f"b{i}":
            errors.append(f"{where}.id must be 'b{i}'")
        if block.get("kind") not in BLOCK_KINDS:
            errors.append(f"{where}.kind must be one of {BLOCK_KINDS}")
        if block.get("role") not in ROLES:
            errors.append(f"{where}.role must be one of {ROLES}")
        if block.get("source") not in SOURCES:
            errors.append(f"{where}.source must be one of {SOURCES}")
        if not isinstance(block.get("tool"), str):
            errors.append(f"{where}.tool must be a string")
        if block.get("kind") == "table":
            cells = block.get("cells")
            if not isinstance(cells, list) or not all(
                isinstance(row, list) and all(isinstance(c, dict) and isinstance(c.get("text"), str) for c in row)
                for row in cells
            ):
                errors.append(f"{where}.cells must be rows of cells with text")
            for row in cells if isinstance(cells, list) else []:
                for c in row if isinstance(row, list) else []:
                    readings = c.get("readings") if isinstance(c, dict) else None
                    if readings is not None and not (
                        isinstance(readings, list)
                        and all(isinstance(r, dict) and isinstance(r.get("model"), str) and isinstance(r.get("text"), str) for r in readings)
                    ):
                        errors.append(f"{where}.cells readings must be objects with a model and a text")
        elif not isinstance(block.get("text"), str):
            errors.append(f"{where}.text must be a string")
        if "half" in block and block["half"] not in ("right", "left"):
            errors.append(f"{where}.half must be 'right' or 'left'")
        if "direction" in block and block["direction"] not in DIRECTIONS:
            errors.append(f"{where}.direction must be one of {DIRECTIONS}")
        if "page" in block and not (isinstance(block["page"], int) and 0 <= block["page"] < max(len(pages), 1)):
            errors.append(f"{where}.page must index pages")
        if "bbox" in block and not _is_box(block["bbox"]):
            errors.append(f"{where}.bbox must be [x0, y0, x1, y1]")
        for line in block.get("lines", []) or []:
            if not (isinstance(line, dict) and isinstance(line.get("text"), str) and _is_box(line.get("bbox"))):
                errors.append(f"{where}.lines must have text and bbox")
                break
        if block.get("source") == "ocr" and not (isinstance(block.get("ocr"), dict) and block["ocr"].get("status") == "unverified"):
            errors.append(f"{where}.ocr must be an object with status 'unverified' for OCR text")
        for ref in block.get("note_refs", []) or []:
            if not (
                isinstance(ref, dict)
                and ref.get("kind") in ("footnote", "endnote")
                and isinstance(ref.get("offset"), int)
                and 0 <= ref["offset"] <= len(block.get("text", ""))
            ):
                errors.append(f"{where}.note_refs must have a kind and an offset inside the text")
                break
        if not isinstance(block.get("repairs"), list):
            errors.append(f"{where}.repairs must be a list")
        if not _strings(block.get("flags")):
            errors.append(f"{where}.flags must be a list of strings")

    unread = payload.get("unread")
    if not isinstance(unread, list):
        errors.append("unread must be a list")
        unread = []
    for i, item in enumerate(unread):
        if not (isinstance(item, dict) and item.get("scope") in UNREAD_SCOPES and isinstance(item.get("reason"), str)):
            errors.append(f"unread[{i}] must have a scope in {UNREAD_SCOPES} and a reason")
        elif "bbox" in item and not _is_box(item["bbox"]):
            errors.append(f"unread[{i}].bbox must be [x0, y0, x1, y1]")
    if not _strings(payload.get("flags")):
        errors.append("flags must be a list of strings")
    for i, entry in enumerate(payload.get("font_maps", [])):
        if not (isinstance(entry, dict) and isinstance(entry.get("font"), str) and isinstance(entry.get("map"), dict)):
            errors.append(f"font_maps[{i}] must have a font and a map")

    # Provenance must be path-free. Block text and cells are content, not provenance.
    provenance = {key: value for key, value in payload.items() if key != "blocks"}
    provenance["blocks"] = [
        {key: value for key, value in block.items() if key not in ("text", "cells", "lines", "label")}
        for block in blocks
        if isinstance(block, dict)
    ]
    for where in _path_like(provenance, "record"):
        errors.append(f"{where} looks like a local filesystem path")
    return errors
