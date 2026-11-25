"""Opt-in label/value associations from exact readings and page geometry."""

from collections import Counter, defaultdict
import re

from heocr.contract import Block, Document, Line
from heocr.table_export import table_grid

_LABEL = re.compile(r"^[^\W\d_][^:\n]{0,48}$", re.UNICODE)
_LABEL_HEADERS = {"field", "label", "question", "item", "שדה", "תווית", "שאלה", "דרישה", "סעיף"}
_VALUE_HEADERS = {"value", "answer", "response", "status", "result", "ערך", "תשובה", "מענה", "ממצא", "מצב"}
_STATUS = {
    "כן", "לא", "תקין", "לא תקין", "חלקי", "מלא", "לא רלוונטי", "לא ישים", "לא נבדק",
    "נבדק", "עומד", "לא עומד", "קיים", "לא קיים", "yes", "no", "pass", "fail",
    "partial", "not applicable", "compliant", "noncompliant",
}


def _normalized(text: str) -> str:
    return " ".join(text.casefold().strip(" :.;").split())


def _label(text: str) -> bool:
    return _LABEL.fullmatch(text) is not None and len(text.split()) <= 6 and all(c.isalpha() or c.isspace()
               or c in "'״׳\"-–" for c in text)


def _pair(label: str, value: str, method: str, **source) -> dict:
    return {"label": label, "value": value, "method": method, **source}


def _headed(grid: list[list[str]]) -> tuple[int, int] | None:
    labels = [i for i, text in enumerate(grid[0]) if _normalized(text) in _LABEL_HEADERS]
    values = [i for i, text in enumerate(grid[0]) if _normalized(text) in _VALUE_HEADERS]
    if len(labels) == len(values) == 1 and labels[0] != values[0]:
        return labels[0], values[0]
    return None


def _repeated_status(grid: list[list[str]]) -> tuple[int, int] | None:
    # Are the decisions in one column and substantial labels in one
    # adjacent column? Require repeated positive row evidence and abstain
    # when a second column pair competes.
    votes = Counter()
    for row in grid:
        for value_col, value in enumerate(row):
            if _normalized(value) not in _STATUS:
                continue
            for label_col in (value_col - 1, value_col + 1):
                if 0 <= label_col < len(row):
                    label = row[label_col].strip()
                    if len(label.split()) >= 4 and any(c.isalpha() for c in label) and _normalized(label) not in _STATUS:
                        votes[label_col, value_col] += 1
    eligible = [pair for pair, count in votes.items() if count >= 3]
    return eligible[0] if len(eligible) == 1 else None


def _table_fields(block: Block) -> list[dict]:
    grid, _ = table_grid(block)
    if not grid or len(grid[0]) < 2:
        return []
    pairs = []
    headed = _headed(grid)
    status = _repeated_status(grid) if headed is None else None
    if columns := headed or status:
        label_col, value_col = columns
        for i, row in enumerate(grid):
            label, value = row[label_col].strip(), row[value_col].strip()
            if not (label and value):
                continue
            if headed and i == 0:
                continue
            if status and (_normalized(value) not in _STATUS or len(label.split()) < 4):
                continue
            pairs.append(_pair(label, value, "table_headers" if headed else "repeated_status_column",
                               row=i, label_column=label_col, value_column=value_col))
        return pairs
    if len(grid[0]) != 2:
        return []
    for index, row in enumerate(grid):
        for label_col, label in enumerate(row):
            label = label.strip()
            value_col = 1 - label_col
            value = row[value_col].strip()
            if label.endswith(":") and _label(label[:-1].strip()) and value and not value.endswith(":"):
                pairs.append(_pair(label[:-1].strip(), value, "column_label_colon",
                                   row=index, label_column=label_col, value_column=value_col))
    return pairs


def _neighbor(label: Line, options: list[Line], rtl: bool) -> Line | None:
    x0, y0, x1, y1 = label.bbox
    height = y1 - y0
    if height <= 0:
        return None
    hits = []
    for line in options:
        a, b, c, d = line.bbox
        span = min(y1, d) - max(y0, b)
        gap = x0 - c if rtl else a - x1
        if 0 <= gap <= 12 * height and span >= .65 * min(height, d - b) and d > b:
            hits.append((gap, line))
    hits.sort(key=lambda item: item[0])
    if not hits or (len(hits) > 1 and hits[1][0] - hits[0][0] <= height):
        return None
    return hits[0][1]


def _add(block: Block, pairs: list[dict]) -> None:
    if pairs:
        block.checks = {**(block.checks or {}), "form_fields": pairs}


def mark(doc: Document) -> None:
    groups = defaultdict(list)
    for block in doc.blocks:
        if block.role != "body":
            continue
        if block.kind == "table" and block.cells:
            _add(block, _table_fields(block))
        elif block.kind == "text" and block.page is not None:
            groups[block.page].extend((block, ordinal, line) for ordinal, line in enumerate(block.lines or []))
    for lines in groups.values():
        values = [line for _, _, line in lines if ":" not in line.text and line.text.strip()]
        picked = set()
        pairs = defaultdict(list)
        owners = {}
        # Explicit colons give label identity. A separated value stays on
        # the same geometry row, on the reading-direction side of the label.
        for block, index, line in lines:
            if ":" not in line.text:
                continue
            label, value = (part.strip() for part in line.text.split(":", 1))
            if not _label(label):
                continue
            method = "inline_colon"
            box = line.bbox
            if not value:
                nearby = _neighbor(line, values, rtl=line.direction == "rtl")
                if nearby is None or id(nearby) in picked:
                    continue
                picked.add(id(nearby))
                value, box, method = nearby.text.strip(), nearby.bbox, "adjacent_colon_line"
            owners[id(block)] = block
            pairs[id(block)].append(_pair(label, value, method, line=index, label_bbox=line.bbox, value_bbox=box))
        # A lone colon in running text does not define a form.
        if sum(len(items) for items in pairs.values()) >= 2:
            for owner, items in pairs.items():
                _add(owners[owner], items)
