"""Opt-in hdoc_form.v1 trees from D3h pairs and corroborated form structure."""

from collections import defaultdict
from dataclasses import replace
import re
from statistics import median

from heocr.contract import Block, Document
from heocr.table_export import table_grid

_BLANK = re.compile(r"[_＿‗]{2,}")
_SPLIT = re.compile(r"\s*:\s*|\s+[-–]\s+")
_MARK = re.compile(r"\[\s*([xX✓✔])?\s*\]|\(\s*([xX✓✔])?\s*\)|[☐☑☒]")
_TITLE = re.compile(r"^(?:טופס|בקשה|form|application)\b", re.IGNORECASE)
_SECTION = re.compile(r"^(?:\d+[.)]\s*)?(?:פרטי|פרטים|section)\b", re.IGNORECASE)
_CONTINUED = re.compile(r"\s*(?:\(\s*(?:המשך|continued)\s*\)|\[\s*(?:המשך|continued)\s*\])$", re.IGNORECASE)
_OCR_CONTINUED = re.compile(r"\s*[\[(]\s*(?:המשך|continued)$", re.IGNORECASE)
_HINTS = {
    "date": {"תאריך", "מועד", "date"},
    "signature": {"חתימה", "חתימת", "signature"},
    "number": {"מספר", "מיקוד", "סכום", "שטח", "כמות", "number", "amount", "zip", "postcode"},
    "text": {"שם", "כתובת", "יישוב", "הערות", "דואר", "name", "address", "email", "notes"},
}


def _hint(label: str) -> str | None:
    tokens = set(re.findall(r"[^\W\d_]+", label.casefold()))
    return next((kind for kind, words in _HINTS.items() if words & tokens), None)


def _label(text: str) -> bool:
    text = text.strip()
    return 0 < len(text.split()) <= 7 and any(ch.isalpha() for ch in text) and not text[0].isdigit()


def _page(block: Block) -> list[int] | None:
    return [block.page + 1] if block.page is not None else None


def _field(block: Block, label: str, value: str, slot: str, kind: str | None = None) -> dict:
    label = label.strip().rstrip(":").rstrip()
    value = value.strip()
    if _BLANK.fullmatch(value):
        value = ""
    if kind is None:
        kind = _hint(label) or "text"
        if kind == "text" and re.fullmatch(r"[+−-]?\d[\d, .%₪$-]*", value):
            kind = "number"
    return {"type": "field", "label": label, "value": value,
            "kind": kind, "slot": slot, "pages": _page(block)}


def _marked(mark: re.Match) -> bool:
    return bool(mark.group(1) or mark.group(2) or mark.group() in {"☑", "☒"})


def _choice(value: str) -> str | None:
    marks = list(_MARK.finditer(value))
    if len(marks) < 2:
        return None
    # Marker-first lists identify the option by the text until the next marker.
    if value[:marks[0].start()].strip():
        return None
    picked = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(value)
        option = value[mark.end():end].strip()
        if not option:
            return None
        if _marked(mark):
            picked.append(option)
    return (picked or [""])[0] if len(picked) <= 1 else None


def _checkbox(block: Block, text: str) -> dict | None:
    marks = list(_MARK.finditer(text))
    if len(marks) != 1 or ":" in text:
        return None
    mark = marks[0]
    before, after = text[:mark.start()].strip(), text[mark.end():].strip()
    if bool(before) == bool(after):
        return None
    label = before or after
    if not any(c.isalpha() for c in label):
        return None
    return _field(block, label, "checked" if _marked(mark) else "unchecked", "box", "checkbox")


def _line_field(block: Block, text: str, pair: dict | None, form_context: bool) -> dict | None:
    if check := _checkbox(block, text):
        return check
    parts = _SPLIT.split(text, maxsplit=1)
    if pair:
        label, value = pair["label"], pair["value"]
    elif form_context and len(parts) == 2 and _label(parts[0]):
        label, value = parts
        if not value and _hint(label) != "signature":
            return None
    else:
        return None
    if mark := _MARK.fullmatch(value.strip()):
        return _field(block, label, "checked" if _marked(mark) else "unchecked", "box", "checkbox")
    if (selected := _choice(value)) is not None:
        return _field(block, label, selected, "line", "choice")
    if len(list(_MARK.finditer(value))) > 1 or (not pair and not _hint(label)):
        return None  # missing field evidence or ambiguous marks
    return _field(block, label, value, "line")


def _recovered_grid_field(block, label, value):
    # A legible label alone does not turn OCR noise into an email address.
    key = " ".join(label.casefold().split())
    if ({"email", "e-mail"} & set(key.split()) or "דואר אלקטרוני" in key) and value:
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()) is None:
            return None
    return _field(block, label, value, "box")


def _mixed_grid_prefix(block, columns):
    """Rows with intact two-cell spans can precede a different, finer grid."""
    rows = block.cells
    if columns < 4 or any(cell.rowspan != 1 for row in rows for cell in row):
        return 0
    prefix = 0
    for row in rows:
        if len(row) != 2 or [cell.colspan * 2 for cell in row] != [columns, columns]:
            break
        label = row[0].text.strip()
        if not (_label(label) and _hint(label)):
            break
        prefix += 1
    if prefix >= 2 and prefix < len(rows) and len(rows[prefix]) > 2:
        return prefix
    return 0


def _table(block: Block, form_context: bool, recover_grid=False) -> list[dict]:
    grid, merges = table_grid(block)
    if not grid or not grid[0]:
        return []
    pairs = (block.checks or {}).get("form_fields", [])
    if pairs:
        return [_field(block, pair["label"], pair["value"], "box") for pair in pairs]
    cols = len(grid[0])
    if recover_grid and form_context and (boundary := _mixed_grid_prefix(block, cols)):
        prefix = [_recovered_grid_field(block, row[0].text, row[1].text) for row in block.cells[:boundary]]
        return [field for field in prefix if field] + _table(replace(block, cells=block.cells[boundary:]), False)
    if form_context and not merges:
        layouts = []
        if len(grid) == 2:
            layouts.append(("cell", [list(zip(*grid))]))
        if cols in (2, 4) and len(grid) >= 2:
            layouts.append(("box", [[(r[i], r[i + 1]) for r in grid] for i in range(0, cols, 2)]))
        ranked = []
        for slot, columns in layouts:
            possible = [(label, value) for column in columns for label, value in column if label or value]
            hints = sum(bool(_hint(label)) and _label(label) for label, _ in possible)
            if hints >= 2 and 2 * hints >= len(possible):
                ranked.append((hints, slot, columns))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
            _, slot, columns = ranked[0]
            children = [_field(block, label, value, slot) for column in columns
                        for label, value in column if _label(label) and (_hint(label) or value)]
            if len(columns) > 1:
                return [{"type": "group", "columns": len(columns), "children": children}]
            return children
    spans = {(r, c): (rs, cs) for r, c, rs, cs in merges}
    covered = {(i, j) for r, c, rs, cs in merges for i in range(r, r + rs)
               for j in range(c, c + cs) if (i, j) != (r, c)}
    cells = []
    for r, row in enumerate(grid):
        for c, text in enumerate(row):
            if (r, c) not in covered:
                rs, cs = spans.get((r, c), (1, 1))
                cells.append({"r": r, "c": c, "rowspan": rs, "colspan": cs, "text": text, "mask": False})
    pages = _page(block)
    return [{"type": "table", "table": {"schema": "heocr_table.v1", "rows": len(grid), "cols": cols,
                "direction": block.direction or "rtl", "header_rows": 0,
                "pages": pages * 2 if pages else None, "cells": cells}}]


def _lines(block: Block):
    return [(line.text.strip(), line.bbox) for line in block.lines] if block.lines else [
        (line.strip(), block.bbox) for line in block.text.splitlines()
    ]


def _field_nodes(children):
    for item in children:
        if item["type"] == "group":
            yield from _field_nodes(item["children"])
        elif item["type"] == "field":
            yield item


def _running_form_title(blocks):
    """A repeated form title may already be tagged as running furniture."""
    pages_by_title = defaultdict(set)
    for block in blocks:
        if block.role != "header" or block.page is None:
            continue
        for text, _ in _lines(block):
            if _TITLE.match(text) and len(text.split()) <= 7:
                pages_by_title[text].add(block.page)
    repeated = [title for title, pages in pages_by_title.items() if any(page + 1 in pages for page in pages)]
    return repeated[0] if len(repeated) == 1 else None


def _continues(section, block, text, box, page_heights):
    if (not section["title"] or not section["children"] or not section["pages"]
            or section["pages"][-1] != block.page or not box
            or not 0 <= box[1] <= .25 * page_heights.get(block.page, 0)):
        return False
    label, count = _CONTINUED.subn("", text)
    if not count and block.source == "ocr":
        label, count = _OCR_CONTINUED.subn("", text)
    if not count:
        return False
    return " ".join(label.casefold().split()) == " ".join(section["title"].casefold().split())


def export(doc: Document, *, join_pages=False) -> list[dict]:
    """Return one supported form per source document, or an empty list."""
    body = [block for block in doc.blocks if block.role == "body"]
    form_context = any(_TITLE.match(text) and len(text.split()) <= 7
                       for block in body for text, _ in _lines(block))
    running_title = _running_form_title(doc.blocks) if join_pages and not form_context else None
    form_context = form_context or running_title is not None
    grid_context = False
    recovered_pairs = {}
    if doc._form_grid_recovery:
        from heocr.pdf.form_grid_rows import aligned_pairs

        headings = {text for block in body for text, _ in _lines(block)
                    if _SECTION.match(text) and ":" not in text and len(text.split()) <= 6}
        grid_context = form_context or len(headings) >= 2
        if grid_context:
            recovered_pairs = aligned_pairs(body, _hint)
    heights = defaultdict(list)
    for block in body:
        for text, box in _lines(block):
            if text and box and box[3] > box[1]:
                heights[block.page].append(box[3] - box[1])
    typical = {page: median(values) for page, values in heights.items()}
    page_heights = {page.index: page.height for page in doc.pages} if join_pages else {}
    title = running_title
    sections = []
    current = {"type": "section", "title": None, "children": [], "pages": None}

    def end_section(next_title, pages=None):
        nonlocal current
        if current["children"]:
            sections.append(current)
        current = {"type": "section", "title": next_title, "children": [], "pages": pages}

    def add(block, children):
        if not children:
            return
        current["children"].extend(children)
        if pages := _page(block):
            current["pages"] = sorted(set(current["pages"] or ()) | set(pages))

    for block in body:
        if block.kind == "table" and block.cells:
            recover_grid = doc._form_grid_recovery and block.source == "ocr"
            add(block, _table(block, form_context or (recover_grid and grid_context), recover_grid))
            continue
        pairs = {pair["line"]: pair for pair in (block.checks or {}).get("form_fields", []) if "line" in pair}
        for ordinal, (text, box) in enumerate(_lines(block)):
            if not text:
                continue
            pair = pairs.get(ordinal)
            field = _line_field(block, text, pair, form_context)
            if form_context and block.lines:
                evidence = block.lines[ordinal]._form_mark_reading
                if evidence and evidence[0].strip() == text:
                    field = _line_field(block, evidence[1], None, form_context) or field
            if field is None and block.lines and (recovered := recovered_pairs.get(id(block.lines[ordinal]))):
                field = _recovered_grid_field(block, *recovered)
            if field:
                if field["kind"] == "signature" and current["title"] is not None and current["children"]:
                    end_section(None)
                add(block, [field])
                continue
            if len(text.split()) > 8 or not any(ch.isalpha() for ch in text) or ":" in text:
                continue
            if _TITLE.match(text) or (block.kind == "heading" and block.level == 1 and title is None):
                title = title or text
                continue
            if join_pages and _continues(current, block, text, box, page_heights):
                current["pages"].extend(_page(block))
                continue
            if block.kind == "heading" or (form_context and (_SECTION.match(text) or (
                    box and block.page in typical and box[3] - box[1] >= 1.18 * typical[block.page]))):
                end_section(text, _page(block))
    end_section(None)
    fields = list(_field_nodes([child for section in sections for child in section["children"]]))
    if not fields:
        return []
    for i, field in enumerate(fields, start=1):
        field["id"] = f"form-1.{i}"
    direction = next((block.direction for block in body if block.direction in {"rtl", "ltr"}), "rtl")
    return [{"schema": "hdoc_form.v1", "id": "form-1", "title": title, "direction": direction,
             "sections": sections}]
