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


def _field_rows(candidates: list[tuple[Block, Line, dict, Line | None]]) -> set[int]:
    """Find neighboring label rows sharing a reading-direction margin."""
    ordered = sorted(enumerate(candidates), key=lambda item: item[1][1].bbox[1])
    supported = set()
    for i, (index, (_, line, _, _)) in enumerate(ordered):
        top, bottom = line.bbox[1], line.bbox[3]
        height = bottom - top
        if height <= 0:
            continue
        edge = 2 if line.direction == "rtl" else 0
        for j in range(i + 1, len(ordered)):
            other_index, (_, other, _, _) = ordered[j]
            gap = other.bbox[1] - bottom
            if gap > 2 * height:
                break
            unit = min(height, other.bbox[3] - other.bbox[1])
            if (unit > 0 and other.direction == line.direction and -0.25 * unit <= gap <= 2 * unit
                    and abs(line.bbox[edge] - other.bbox[edge]) <= 1.5 * unit):
                supported.update((index, other_index))
    return supported


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
        labels = [(block, ordinal, line) for block, ordinal, line in lines if ":" in line.text]
        if len(labels) < 2:
            continue
        values = None
        candidates = []
        # Explicit colons give label identity. A separated value stays on
        # the same geometry row, on the reading-direction side of the label.
        for block, index, line in labels:
            label, value = (part.strip() for part in line.text.split(":", 1))
            if not _label(label):
                continue
            method = "inline_colon"
            box = line.bbox
            nearby = None
            if not value:
                if values is None:
                    values = [other for _, _, other in lines if ":" not in other.text and other.text.strip()]
                nearby = _neighbor(line, values, rtl=line.direction == "rtl")
                if nearby is None:
                    continue
                value, box, method = nearby.text.strip(), nearby.bbox, "adjacent_colon_line"
            pair = _pair(label, value, method, line=index, label_bbox=line.bbox, value_bbox=box)
            candidates.append((block, line, pair, nearby))
        # Colons scattered through prose do not corroborate one another.
        supported = _field_rows(candidates)
        picked = Counter(id(nearby) for i, (_, _, _, nearby) in enumerate(candidates)
                         if i in supported and nearby is not None)
        by_block = defaultdict(list)
        owners = {}
        for i, (block, _, pair, nearby) in enumerate(candidates):
            if i in supported and (nearby is None or picked[id(nearby)] == 1):
                owners[id(block)] = block
                by_block[id(block)].append(pair)
        for owner, items in by_block.items():
            _add(owners[owner], items)
