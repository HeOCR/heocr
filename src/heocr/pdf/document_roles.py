"""Conservative margin roles and physical page order for paginated OCR and PDFs."""

from collections import defaultdict
from dataclasses import replace
from itertools import groupby
import re

from heocr.contract import Block, Document, Line, Page

_ROLES = {"header": 0, "footnote": 2, "footer": 3}
_NOTE_START = re.compile(r"^\s*(?:[\[ (]?\d{1,2}[\].)]?|[*†‡]|[¹²³⁴⁵⁶⁷⁸⁹])\s+[^\W\d_]", re.UNICODE)


def _split_safe(block: Block) -> bool:
    return (block.kind == "text" and block.role == "body" and block.source in {"ocr", "text_layer"}
            and bool(block.lines) and not (block.note_refs or block.label or block.note_id)
            and block.text == "\n".join(line.text for line in block.lines))


def _role(line: Line, page: Page, body_top: float, body_bottom: float) -> str:
    x0, y0, x1, y1 = line.bbox
    height = page.height
    if not line.text.strip() or not 0 < y1 - y0 <= .026 * height:
        return "body"
    if y1 <= .068 * height and x1 - x0 <= .70 * page.width and body_top - y1 >= .015 * height:
        return "header"
    if y0 >= .945 * height and x1 - x0 <= .65 * page.width and y0 - body_bottom >= .022 * height:
        return "footer"
    if .86 * height <= y0 < .945 * height and y0 - body_bottom >= .032 * height and _NOTE_START.match(line.text):
        return "footnote"
    return "body"


def _group(block: Block, marks: list[str]) -> list[Block]:
    if len(set(marks)) == 1:
        role = marks[0]
        return [replace(block, role=role, flags=[*block.flags, "page_furniture"])] if role != "body" else [block]
    pieces = []
    for role, pairs in groupby(zip(marks, block.lines, strict=True), key=lambda pair: pair[0]):
        lines = [line for _, line in pairs]
        bbox = [fn(line.bbox[i] for line in lines) for i, fn in enumerate((min, min, max, max))]
        flags = [*block.flags, "document_role_line_split"]
        if role != "body":
            flags.append("page_furniture")
        pieces.append(replace(block, text="\n".join(line.text for line in lines), lines=lines, bbox=bbox,
                              role=role, flags=flags, checks=None, repairs=[]))
    # A block-level log has no line offsets. Keep it once, with the body when possible.
    keep = next((part for part in pieces if part.role == "body"), pieces[0])
    keep.checks = block.checks
    keep.repairs = block.repairs
    return pieces


def _arrange(blocks: list[Block], page: Page, rtl: bool) -> list[Block]:
    candidates = [b for b in blocks if _split_safe(b)]
    # The middle text and any intervening table define where the body ends.
    middle = [line.bbox for block in candidates for line in block.lines
              if line.bbox[3] > .068 * page.height and line.bbox[1] < .86 * page.height]
    middle.extend(b.bbox for b in blocks if b.kind == "table" and b.bbox and b.bbox[1] < .86 * page.height)
    if len(middle) < 2:
        return blocks
    top = min(box[1] for box in middle)
    bottom = max(box[3] for box in middle)
    new = []
    changed = False
    for block in blocks:
        if _split_safe(block):
            marks = [_role(line, page, top, bottom) for line in block.lines]
            if any(role != "body" for role in marks):
                new.extend(_group(block, marks))
                changed = True
                continue
        new.append(block)
    if not changed and not any(block.role in _ROLES for block in new):
        return blocks
    page.flags = [*page.flags, "document_roles_ordered"]
    # Preserve the established reading order within the body. Order tagged
    # furniture by position, with the declared direction for tied rows.
    def key(block):
        rank = _ROLES.get(block.role, 1)
        box = block.bbox
        return (rank, box[1], -box[2] if rtl else box[0]) if rank != 1 and box else (rank, 0, 0)
    return sorted(new, key=key)


def apply(doc: Document) -> None:
    capture = (doc._ocr_settings or {}).get("capture_type")
    if capture in {"phone", "camera-photo", "pixel", "iphone"}:
        return
    pages = {page.index: page for page in doc.pages if page.width > 0 and page.height > 0}
    groups = defaultdict(list)
    for block in doc.blocks:
        if block.page in pages:
            groups[block.page].append(block)
    ordered = {i: _arrange(blocks, pages[i], doc._rtl) for i, blocks in groups.items()}
    emitted = set()
    result = []
    for block in doc.blocks:
        if block.page not in ordered:
            result.append(block)
        elif block.page not in emitted:
            result.extend(ordered[block.page])
            emitted.add(block.page)
    doc.blocks[:] = result
