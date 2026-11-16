"""Plain-text and Markdown views of a ``Document``.

The body comes first, in reading order. Notes, comments, headers and footers follow
under their own separators: they are never mixed into the body text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from heocr.contract import Block, Document

# Roles that stay in the body flow, and the order of the separate sections after it.
_INLINE_ROLES = ("body", "textbox")
_SECTIONS = (
    ("footnote", "Footnotes"),
    ("endnote", "Endnotes"),
    ("comment", "Comments"),
    ("header", "Headers"),
    ("footer", "Footers"),
)
_MD_LIST_MARKER = re.compile(r"\A(\d+)([.)])\Z")


def _labelled(block: "Block") -> str:
    if not block.label:
        return block.text
    joiner = "" if "label_suffix_nothing" in block.flags else " "
    return f"{block.label}{joiner}{block.text}"


def _table_text(block: "Block") -> str:
    rows = []
    for row in block.cells or []:
        rows.append(" | ".join(" ".join(cell.text.split("\n")) for cell in row))
    return "\n".join(rows)


def _block_text(block: "Block") -> str:
    if block.kind == "table":
        return _table_text(block)
    if block.kind == "figure":
        return ""
    return _labelled(block)


def _note_line(block: "Block", body: str) -> str:
    return f"[{block.note_id}] {body}" if block.note_id else body


def to_text(doc: "Document") -> str:
    parts: list[str] = []
    previous_page: int | None = None
    for block in doc.blocks:
        if block.role not in _INLINE_ROLES:
            continue
        if previous_page is not None and block.page is not None and block.page != previous_page:
            parts.append("\f")
        previous_page = block.page if block.page is not None else previous_page
        text = _block_text(block)
        if text:
            parts.append(text)
    body = "\n".join(parts).replace("\n\f\n", "\f")
    sections = []
    for role, title in _SECTIONS:
        items = [_note_line(b, _block_text(b)) for b in doc.blocks if b.role == role and _block_text(b)]
        if items:
            sections.append(f"--- {title} ---\n" + "\n".join(items))
    out = "\n\n".join([body, *sections]) if sections else body
    return out + "\n" if out else ""


def _md_escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def _md_table(block: "Block") -> str:
    rows = []
    for row in block.cells or []:
        out: list[str] = []
        for cell in row:
            # Keep grid columns aligned: a spanned cell is followed by empty cells.
            out.append(_md_escape_cell(cell.text))
            out.extend([""] * (cell.colspan - 1))
        rows.append(out)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def _md_block(block: "Block") -> str:
    if block.kind == "table":
        return _md_table(block)
    if block.kind == "figure":
        return "<!-- heocr: figure not read -->"
    label = block.label
    text = block.text
    for ref in sorted(block.note_refs or [], key=lambda r: -r["offset"]):
        # Footnote references as Markdown footnotes, tied to their place; endnotes keyed "e<id>".
        key = ref["id"] if ref["kind"] == "footnote" else f"e{ref['id']}"
        text = text[: ref["offset"]] + f"[^{key}]" + text[ref["offset"] :]
    if label and _MD_LIST_MARKER.match(label):
        # "2." at the start of a line would become a Markdown list and be renumbered.
        label = _MD_LIST_MARKER.sub(r"\1\\\2", label)
    if label:
        joiner = "" if "label_suffix_nothing" in block.flags else " "
        text = f"{label}{joiner}{text}"
    if block.kind == "heading":
        return "#" * min(max(block.level or 1, 1), 6) + " " + text.replace("\n", " ")
    # Hard line breaks inside one block.
    return text.replace("\n", "  \n")


def to_markdown(doc: "Document") -> str:
    parts: list[str] = []
    previous_page: int | None = None
    for block in doc.blocks:
        if block.role not in _INLINE_ROLES:
            continue
        if block.page is not None and block.page != previous_page:
            parts.append(f"<!-- page {block.page + 1} -->")
            previous_page = block.page
        md = _md_block(block)
        if md:
            parts.append(md)
    for role, title in _SECTIONS:
        items = [b for b in doc.blocks if b.role == role]
        if not items:
            continue
        parts.append(f"---\n\n**{title}**")
        for b in items:
            md = _md_block(b)
            if role in ("footnote", "endnote") and b.note_id:
                key = b.note_id if role == "footnote" else f"e{b.note_id}"
                md = f"[^{key}]: {md}"
            parts.append(md)
    for item in doc.unread:
        where = f" page {item.page + 1}" if item.page is not None else ""
        detail = f": {item.detail}" if item.detail else ""
        parts.append(f"<!-- heocr: unread{where} ({item.reason}){detail} -->")
    return "\n\n".join(parts) + "\n" if parts else ""
