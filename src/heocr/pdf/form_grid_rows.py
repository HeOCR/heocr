"""Recover paired OCR rows from repeated label and value geometry."""

from collections import defaultdict
from dataclasses import dataclass
from statistics import median
import re

# A physical cell gap is appreciably larger than ordinary inter-word space.
_LABEL = re.compile(r"^[^\W\d_][\w\s'״׳\"-]*$")


@dataclass(frozen=True)
class _Row:
    label: str
    value: str
    label_edge: float
    value_edge: float
    bottom: float
    height: float


def _row(line, known_label):
    words = line._form_grid_words
    if line.direction != "rtl" or len(words) < 2 or line.text != " ".join(text for text, _ in words):
        return None
    height = median(y1 - y0 for _, (_, y0, _, y1) in words)
    if height <= 0:
        return None
    gaps = [(words[i - 1][1][0] - words[i][1][2], i) for i in range(1, len(words))]
    gap, at = max(gaps)
    if gap < 2.5 * height or any(other >= .65 * gap for other, i in gaps if i != at):
        return None
    label = " ".join(text for text, _ in words[:at])
    if at > 4 or not _LABEL.fullmatch(label) or not known_label(label):
        return None
    value = " ".join(text for text, _ in words[at:])
    if not any(ch.isalnum() for ch in value) or known_label(value):
        return None
    first, second = words[0][1], words[at][1]
    if abs(first[3] - second[3]) > height:
        return None
    return _Row(label, value, first[2], second[2], max(first[3], second[3]), height)


def aligned_pairs(blocks, known_label):
    """Use only isolated wide gaps repeated within a single page's pair of lanes."""
    pages = defaultdict(list)
    for block in blocks:
        if block.kind != "text" or block.page is None:
            continue
        for line in block.lines or ():
            if row := _row(line, known_label):
                pages[block.page].append((line, row))

    pairs = {}
    for found in pages.values():
        ordered = sorted(found, key=lambda item: item[1].bottom)
        for i, (line, row) in enumerate(ordered):
            for j in range(i + 1, len(ordered)):
                other, neighbor = ordered[j]
                gap = neighbor.bottom - row.bottom
                if gap > 5 * row.height:
                    break
                unit = min(row.height, neighbor.height)
                if (row.label == neighbor.label or not .7 * unit <= gap <= 5 * unit
                        or max(abs(neighbor.label_edge - row.label_edge),
                               abs(neighbor.value_edge - row.value_edge)) > 1.4 * unit):
                    continue
                pairs[id(line)] = row.label, row.value
                pairs[id(other)] = neighbor.label, neighbor.value
    return pairs
