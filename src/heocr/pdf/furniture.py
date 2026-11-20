"""Page furniture: running headers, footers and page numbers get the header/footer roles (D3a).

Furniture is a role, not a deletion. The text stays in the record, in blocks whose role is
``header`` or ``footer``, and only leaves the body flow, as Word's own headers and footers do
in DOCX (DOCX is not detected twice).

A text-layer line is furniture when all three hold:

- it lies wholly inside the top or bottom ``BAND`` of its page;
- the same text pattern repeats in the same band, at about the same horizontal place.
  Digits count as one class, so page numbers and "עמוד 3 מתוך 12" group together;
- it repeats on at least ``MIN_PAGES`` pages, or on a majority (at least 2) of the
  document's pages. A body line that happens to repeat, such as a form heading, is then
  not taken for furniture unless it keeps to the margin band;
- a long line (more than ``LONG_WORDS`` words) varies in at most one of its numbers across
  those pages: a running header changes only its page number. A body sentence of the same
  form on short pages ("הסכום הכולל לא יעלה על 527,000 ₪, עד ליום ...") changes its amount
  and its date, and stays in the body.

Once furniture leaves the body, a page whose side-by-side "columns" were only a header next
to a page number no longer carries ``reading_order_unresolved_columns`` (or, since D3c, the
``reading_order_row_major_unverified`` that such a row gets).
"""

from __future__ import annotations

import re
from collections import defaultdict

from heocr.contract import Block, Document

BAND = 0.1
MIN_PAGES = 3
X_BIN_PT = 36.0
LONG_WORDS = 8

_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def _pattern(text: str) -> str:
    return _SPACES.sub(" ", _DIGITS.sub("#", text)).strip()


def _band(bbox, height: float) -> str | None:
    if bbox[3] <= BAND * height:
        return "header"
    if bbox[1] >= (1 - BAND) * height:
        return "footer"
    return None


def _numbers_fit(pattern: str, numbers: list[list[str]]) -> bool:
    """Whether a repeated line's numbers vary as furniture's do: in a long line, at most one of
    them changes across its repeats (a page number)."""
    if len(pattern.split()) <= LONG_WORDS:
        return True
    varying = sum(1 for values in zip(*numbers) if len(set(values)) > 1)
    return varying <= 1


def mark_furniture(doc: Document) -> int:
    """Give repeated margin lines the header/footer roles. Returns the number of lines moved."""
    heights = {page.index: page.height for page in doc.pages}
    total = len(doc.pages)
    seen: dict[tuple, set[int]] = defaultdict(set)
    numbers: dict[tuple, list[list[str]]] = defaultdict(list)  # each repeat's numbers, in order
    keyed: list[tuple[int, int, tuple]] = []  # (block index, line index, key)
    for b, block in enumerate(doc.blocks):
        if block.source != "text_layer" or block.role != "body" or block.page is None or not block.lines:
            continue
        for i, line in enumerate(block.lines):
            band = _band(line.bbox, heights.get(block.page, 0.0))
            if band is None or not line.text.strip():
                continue
            key = (band, _pattern(line.text), round((line.bbox[0] + line.bbox[2]) / 2 / X_BIN_PT))
            seen[key].add(block.page)
            numbers[key].append(_DIGITS.findall(line.text))
            keyed.append((b, i, key))
    accepted = {
        key for key, pages in seen.items()
        if (len(pages) >= MIN_PAGES or (len(pages) >= 2 and len(pages) > total / 2)) and _numbers_fit(key[1], numbers[key])
    }
    if not accepted:
        return 0
    furniture: dict[int, dict[int, str]] = defaultdict(dict)
    for b, i, key in keyed:
        if key in accepted:
            furniture[b][i] = key[0]
    blocks: list[Block] = []
    moved = 0
    for b, block in enumerate(doc.blocks):
        marks = furniture.get(b)
        if not marks:
            blocks.append(block)
            continue
        body = [line for i, line in enumerate(block.lines) if i not in marks]
        parts = []
        for role in ("header", "footer"):
            lines = [line for i, line in enumerate(block.lines) if marks.get(i) == role]
            if lines:
                moved += len(lines)
                parts.append(_sub_block(block, lines, role))
        if body:
            parts.append(_sub_block(block, body, "body"))
        # The block's repair log and number checks are kept whole, on its body part, or on its
        # only part: lines do not carry their own, and a log split by guess would be wrong.
        keeper = parts[-1] if body else parts[0]
        keeper.repairs = list(block.repairs)
        keeper.checks = block.checks
        blocks.extend(parts)
    doc.blocks[:] = _ordered(blocks)
    _clear_furniture_columns(doc)
    return moved


def _sub_block(block: Block, lines, role: str) -> Block:
    bbox = [min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines), max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)]
    flags = list(block.flags) + (["page_furniture"] if role != "body" else [])
    return Block(
        kind=block.kind,
        text="\n".join(l.text for l in lines),
        role=role,
        source=block.source,
        tool=block.tool,
        page=block.page,
        bbox=bbox,
        direction=block.direction,
        lines=list(lines),
        flags=flags,
    )


def _ordered(blocks: list[Block]) -> list[Block]:
    """Keep each page's blocks together, in reading order, headers first and footers last."""
    rank = {"header": 0, "body": 1, "textbox": 1, "footer": 2}
    order = {id(b): n for n, b in enumerate(blocks)}
    return sorted(blocks, key=lambda b: (b.page if b.page is not None else -1, rank.get(b.role, 1), order[id(b)]))


def _clear_furniture_columns(doc: Document, reason: str = "columns_were_page_furniture") -> None:
    """Drop the side-by-side flags where only furniture (or a site menu, D4g) stood side by side."""
    side_flags = ("reading_order_unresolved_columns", "reading_order_row_major_unverified")
    body_lines = defaultdict(list)
    for block in doc.blocks:
        if block.source == "text_layer" and block.role == "body" and block.page is not None:
            body_lines[block.page].extend(block.lines or [])
    for page in doc.pages:
        if not any(f in page.flags for f in side_flags):
            continue
        lines = body_lines.get(page.index, [])
        side_by_side = any(
            min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1]) > 0.5 * min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
            for n, a in enumerate(lines)
            for b in lines[n + 1 :]
        )
        if not side_by_side:
            page.flags = [f for f in page.flags if f not in side_flags] + [reason]
            if "reading_order_invalid_geometry" not in page.flags:
                page.coverage["selection_safe"] = True
