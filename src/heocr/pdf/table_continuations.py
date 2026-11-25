"""Link adjacent physical table fragments with corroborated repeated headers."""

from collections import defaultdict
import unicodedata

from heocr.contract import Block, Document, Page
from heocr.table_export import table_grid


def _header(block: Block) -> tuple[str, ...] | None:
    if not block.cells or len(block.cells) < 2:
        return None
    if any(cell.rowspan != 1 or cell.colspan != 1 or "merged_above" in cell.flags for cell in block.cells[0]):
        return None
    if any(cell.rowspan > len(block.cells) - row for row, cells in enumerate(block.cells) for cell in cells):
        return None
    grid, _ = table_grid(block)
    header = tuple(" ".join(unicodedata.normalize("NFC", cell).casefold().split()) for cell in grid[0])
    labels = {text for text in header if any(c.isalpha() for c in text)}
    if len(labels) < 2 or not any(any(cell.strip() for cell in row) for row in grid[1:]):
        return None
    return header


def _edge(blocks: list[Block], page: Page, *, bottom: bool) -> Block | None:
    # Page-edge support plus a unique table. Repeated headers alone are
    # common in reports with unrelated tables on successive pages.
    eligible = [b for b in blocks if b.kind == "table" and b.role == "body" and b.bbox and not b.half
                and (b.bbox[3] >= .80 * page.height if bottom else b.bbox[1] <= .20 * page.height)]
    if len(eligible) != 1:
        return None
    table = eligible[0]
    edge = table.bbox[3] if bottom else table.bbox[1]
    allowance = .045 * page.height
    for block in blocks:
        if block is table or not block.bbox or block.role not in {"body", "textbox"}:
            continue
        if bottom and block.bbox[1] > edge + 2 and block.bbox[1] < page.height - allowance:
            return None
        if not bottom and allowance < block.bbox[3] < edge - 2:
            return None
    return table


def _compatible(earlier: Block, later: Block, left: Page, right: Page) -> bool:
    if earlier.direction != later.direction or earlier.source != later.source:
        return False
    if abs(left.width - right.width) > .04 * max(left.width, right.width):
        return False
    if any(abs(earlier.bbox[i] / left.width - later.bbox[i] / right.width) > .025 for i in (0, 2)):
        return False
    first = _header(earlier)
    return first is not None and first == _header(later)


def mark(doc: Document) -> None:
    """Attach stable group/part metadata, keeping physical table blocks as read."""
    if len(doc.pages) < 2 or not any(b.kind == "table" for b in doc.blocks):
        return
    pages = {p.index: p for p in doc.pages if p.width > 0 and p.height > 0}
    blocks = defaultdict(list)
    for block in doc.blocks:
        if block.page in pages:
            blocks[block.page].append(block)
    groups = {}
    for index in sorted(pages):
        if index + 1 not in pages:
            continue
        left, right = pages[index], pages[index + 1]
        earlier = _edge(blocks[index], left, bottom=True)
        later = _edge(blocks[index + 1], right, bottom=False)
        if earlier is None or later is None or not _compatible(earlier, later, left, right):
            continue
        if id(earlier) in groups:
            group, part = groups[id(earlier)]
        else:
            group, part = f"table-p{index + 1}", 0
            groups[id(earlier)] = (group, part)
        groups[id(later)] = (group, part + 1)
    for block in doc.blocks:
        if id(block) not in groups:
            continue
        group, part = groups[id(block)]
        block.checks = {**(block.checks or {}), "table_continuation": {
            "group": group, "part": part, "repeated_header_rows": int(part > 0),
        }}
        block.flags = [*block.flags, "table_continuation"]
