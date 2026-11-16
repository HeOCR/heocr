"""Web pages printed to PDF (D4g): the site menu on the first page gets the ``navigation`` role.

A browser print is recognised only by its running header and footer, which D3a already gives
the ``header`` and ``footer`` roles:

- a **page counter** "n/m" in the furniture, whose n is the page's own number and whose m is
  the document's page count, on at least two pages;
- and a **URL** (``http://`` or ``https://``) in the furniture, or a **date with a time** in one
  furniture line, as browsers print them ("16/11/2026, 18:41"). A date alone is not enough:
  Word documents, tenders among them, print "1/12" in the footer and a date in the header.

On such a document, the page's content starts with its title, the first line on the first page
that is either:

- at least ``TITLE_HEIGHT`` times the document's median body line height, and the start of a
  run of at most ``TITLE_LINES`` lines holding at least half the words of the running title
  (the browser prints the page's ``<title>`` in its header);
- or, when the furniture has no title, at least ``HEADING_HEIGHT`` times the median.

Above it is the site menu: at least ``MIN_MENU_PIECES`` pieces, each of at most
``MENU_MAX_WORDS`` words (rows of short links, the site's name, a breadcrumb). A piece is a
line, or a table cell when the menu's aligned rows were read as a table. They get the role
``navigation`` and the flag ``site_menu``. Navigation is a role, not a deletion: the
text stays in the record, out of the body flow. Without the evidence, or when a longer piece or
a figure stands above the title, nothing changes.
"""

from __future__ import annotations

import re
import statistics

from heocr.contract import Block, Document

TITLE_HEIGHT = 1.3
HEADING_HEIGHT = 1.6
TITLE_LINES = 3
TITLE_SHARE = 0.5
MIN_MENU_PIECES = 3
MENU_MAX_WORDS = 6
MIN_COUNTER_PAGES = 2

_COUNTER = re.compile(r"\s*(\d{1,4})\s*/\s*(\d{1,4})\s*")
_URL = re.compile(r"\bhttps?://\S+", re.I)
_DATE = re.compile(r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})(?!\d)")
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_WORD = re.compile(r"[^\W_]+")


def _furniture_lines(doc: Document):
    for block in doc.blocks:
        if block.role in ("header", "footer") and block.page is not None:
            for line in block.lines or []:
                yield block.role, block.page, line.text


def browser_print(doc: Document) -> bool:
    """A running page counter n/m on at least two pages, with a URL, or a date and a time, in the furniture."""
    total = doc.page_count or len(doc.pages)
    counted, stamped = set(), False
    for _, page, text in _furniture_lines(doc):
        match = _COUNTER.fullmatch(text)
        if match and int(match.group(1)) == page + 1 and int(match.group(2)) == total:
            counted.add(page)
        elif _URL.search(text) or (_DATE.search(text) and _TIME.search(text)):
            # A date alone is not enough: Word documents print "n/m" with a date in the header.
            stamped = True
    return len(counted) >= MIN_COUNTER_PAGES and stamped


def _running_title(doc: Document) -> set[str]:
    """The words of the longest header line on the first page that is not a URL, date or counter."""
    best: list[str] = []
    for role, page, text in _furniture_lines(doc):
        if role != "header" or page != 0 or _COUNTER.fullmatch(text) or _URL.search(text) or _DATE.search(text) or _TIME.search(text):
            continue
        words = _WORD.findall(text)
        if len(words) > len(best):
            best = words
    return set(best) if len(best) >= 2 else set()


def mark_site_menu(doc: Document) -> int:
    """On a browser print, give the first page's site menu the navigation role. Returns the pieces moved."""
    if not doc.pages or not browser_print(doc):
        return 0
    doc.flags.append("browser_print")
    first = [b for b in doc.blocks if b.page == 0 and b.role == "body"]
    lines = sorted(
        ((line, block) for block in first if block.source == "text_layer" for line in block.lines or []),
        key=lambda pair: (pair[0].bbox[1], -pair[0].bbox[2]),
    )
    heights = [l.bbox[3] - l.bbox[1] for b in doc.blocks if b.role == "body" and b.source == "text_layer" for l in b.lines or []]
    if not lines or not heights:
        return 0
    median = statistics.median(heights)
    title = _running_title(doc)
    start = None
    for n, (line, _) in enumerate(lines):
        height = line.bbox[3] - line.bbox[1]
        if title:
            if height < TITLE_HEIGHT * median:
                continue
            words = {w for l, _ in lines[n : n + TITLE_LINES] for w in _WORD.findall(l.text)}
            if len(words & title) >= TITLE_SHARE * len(title):
                start = line
                break
        elif height >= HEADING_HEIGHT * median:
            start = line
            break
    if start is None:
        return 0
    top = start.bbox[1]
    above = [(line, block) for line, block in lines if line.bbox[3] <= top + 0.5]
    tables = [b for b in first if b.kind == "table" and b.bbox is not None and b.bbox[3] <= top + 0.5]
    cells = [c.text for t in tables for row in t.cells or [] for c in row if c.text.strip()]
    figures = any(b.kind == "figure" and b.bbox is not None and b.bbox[3] <= top + 0.5 for b in first)
    pieces = [line.text for line, _ in above] + cells
    # A menu of aligned rows may have been read as a table (D3c): its cells are pieces too.
    if len(pieces) < MIN_MENU_PIECES or figures or any(len(piece.split()) > MENU_MAX_WORDS for piece in pieces):
        return 0
    menu = {id(line) for line, _ in above}
    for table in tables:
        table.role = "navigation"
        table.flags.append("site_menu")
    from heocr.pdf.furniture import _clear_furniture_columns, _sub_block

    out: list[Block] = []
    for block in doc.blocks:
        if block.page != 0 or block.role != "body" or not block.lines or not any(id(l) in menu for l in block.lines):
            out.append(block)
            continue
        nav = [l for l in block.lines if id(l) in menu]
        body = [l for l in block.lines if id(l) not in menu]
        parts = [_sub_block(block, nav, "navigation")]
        parts[0].flags = [f for f in parts[0].flags if f != "page_furniture"] + ["site_menu"]
        if body:
            parts.append(_sub_block(block, body, "body"))
        keeper = parts[-1]
        keeper.repairs = list(block.repairs)
        keeper.checks = block.checks
        out.extend(parts)
    doc.blocks[:] = out
    doc.pages[0].flags.append("site_menu")
    _clear_furniture_columns(doc, "columns_were_site_menu")
    return len(pieces)
