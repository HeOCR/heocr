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
from difflib import SequenceMatcher

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


def _ocr_header_row(anchor: Block, blocks: list[Block], rtl: bool) -> list[Block]:
    """Take the contiguous fragments in the anchor's visual row."""
    _, top, _, bottom = anchor.bbox
    height = bottom - top
    row = sorted((b for b in blocks if min(bottom, b.bbox[3]) - max(top, b.bbox[1])
                  >= 0.5 * min(height, b.bbox[3] - b.bbox[1])), key=lambda b: b.bbox[0])
    left = right = next(i for i, b in enumerate(row) if b is anchor)
    while left > 0 and row[left].bbox[0] - row[left - 1].bbox[2] <= height:
        left -= 1
    while right + 1 < len(row) and row[right + 1].bbox[0] - row[right].bbox[2] <= height:
        right += 1
    return row[left:right + 1][::(-1 if rtl else 1)]


def _ocr_header_text(text: str) -> str:
    if any(ch.isdigit() for ch in text) or len(text.split()) < 2:
        return ""
    normalized = "".join(ch for ch in text.casefold() if ch.isalpha())
    return normalized if 7 <= len(normalized) <= 60 else ""


def mark_ocr_running_headers(doc: Document) -> None:
    """Move a recurring centered scan header and its neighboring OCR fragments out of the body.

    Require textual support on three nearby pages in the same margin position. Never split an
    OCR block: doing so would detach its confidence and second readings from their source.
    """
    pages = {p.index: p for p in doc.pages if p.route == "ocr" and p.width > 0 and p.height > 0}
    candidates: dict[int, list[Block]] = defaultdict(list)
    for block in doc.blocks:
        page = pages.get(block.page)
        if page is None or block.source != "ocr" or block.role != "body" or not block.bbox or not block.lines:
            continue
        x0, y0, x1, y1 = block.bbox
        if (0.32 * page.width <= x0 < x1 <= 0.68 * page.width and y1 <= 0.075 * page.height
                and 0 < y1 - y0 <= 0.022 * page.height):
            candidates[page.index].append(block)

    anchors: dict[int, list[tuple[list[Block], set[str], float, float]]] = {}
    for index, blocks in candidates.items():
        page = pages[index]
        anchors[index] = []
        if len(blocks) > 16:  # Too many fragments to identify a single running title confidently.
            continue
        for block in blocks:
            if any(ch.isdigit() for ch in block.text) or not any(ch.isalpha() for ch in block.text):
                continue
            row = _ocr_header_row(block, blocks, doc._rtl)
            joined = " ".join(b.text for b in row if not any(ch.isdigit() for ch in b.text))
            texts = {_ocr_header_text(block.text), _ocr_header_text(joined)} - {""}
            x0, y0, x1, y1 = block.bbox
            if texts:
                anchors[index].append((row, texts, (x0 + x1) / (2 * page.width), (y0 + y1) / (2 * page.height)))

    headers = []
    for index, items in anchors.items():
        supported = []
        for row, texts, x, y in items:
            repeats = sum(
                any(abs(x - xx) <= 0.10 and abs(y - yy) <= 0.025
                    and any(SequenceMatcher(None, text, other, autojunk=False).ratio() >= 0.75
                            for text in texts for other in other_texts)
                    for _, other_texts, xx, yy in anchors.get(i, ()))
                for i in range(index - 4, index + 5) if i != index
            )
            if repeats >= 2:
                supported.append((repeats, max(map(len, texts)), row))
        if not supported:
            continue
        row = max(supported, key=lambda item: item[:2])[2]
        for block in row:
            block.role = "header"
            block.flags.append("page_furniture")
        headers.extend(row)

    if headers:
        selected = {id(b) for b in headers}
        doc.blocks[:] = _ordered([*headers, *(b for b in doc.blocks if id(b) not in selected)])


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
