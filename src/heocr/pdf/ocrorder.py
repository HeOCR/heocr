"""Reconnect lines separated in OCR output by a disjoint text track."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterator

from heocr.contract import Block, Line

TRACK_OVERLAP = 0.65
TRACK_MAX_GAP_HEIGHTS = 2.5
TRACK_MAX_RAGGED_HEIGHTS = 3.0
TRACK_AMBIGUOUS_ROW_SHARE = 0.4


def _same_track(upper: Line, lower: Line) -> bool:
    left, top, right, bottom = upper.bbox
    low_left, low_top, low_right, low_bottom = lower.bbox
    height = max(bottom - top, low_bottom - low_top)
    overlap = min(right, low_right) - max(left, low_left)
    return (0 <= low_top - bottom <= TRACK_MAX_GAP_HEIGHTS * height
            and overlap >= TRACK_OVERLAP * min(right - left, low_right - low_left)
            and abs(right - low_right) <= TRACK_MAX_RAGGED_HEIGHTS * height)


def _neighbors(lines: list[Line]) -> Iterator[tuple[int, int]]:
    by_y = sorted(range(len(lines)), key=lambda i: lines[i].bbox[1])
    tops = [lines[i].bbox[1] for i in by_y]
    reach = TRACK_MAX_GAP_HEIGHTS * max(line.bbox[3] - line.bbox[1] for line in lines)
    for i, upper in enumerate(lines):
        candidates = []
        bottom = upper.bbox[3]
        for k in range(bisect_left(tops, bottom), bisect_right(tops, bottom + reach)):
            j = by_y[k]
            if _same_track(upper, lines[j]):
                candidates.append(j)
                if len(candidates) == 2:
                    break
        if not candidates:
            continue
        j = candidates[0]
        if len(candidates) == 2:
            _, top, _, lower_bottom = lines[j].bbox
            if lines[candidates[1]].bbox[1] - top < TRACK_AMBIGUOUS_ROW_SHARE * (lower_bottom - top):
                continue
        yield i, j


def _disjoint_gap(lines: list[Line], owners: list[int], start: int, end: int) -> bool:
    if start + 1 == end:
        return True
    if end <= start or owners[start] == owners[end]:
        return False
    left = min(lines[start].bbox[0], lines[end].bbox[0])
    right = max(lines[start].bbox[2], lines[end].bbox[2])
    return all(line.bbox[2] <= left or line.bbox[0] >= right for line in lines[start + 1:end])


def order_across_disjoint_gaps(blocks: list[Block]) -> list[tuple[int, Line]] | None:
    """Return a stable forward permutation of body OCR lines, retaining block ownership."""
    if len(blocks) < 2 or any(
        block.kind != "text" or block.role != "body" or block.source != "ocr" or not block.lines
        for block in blocks
    ):
        return None
    ordered = [(i, line) for i, block in enumerate(blocks) for line in block.lines]
    owners = [owner for owner, _ in ordered]
    lines = [line for _, line in ordered]
    if any(line.bbox[2] <= line.bbox[0] or line.bbox[3] <= line.bbox[1] for line in lines):
        return None
    candidates = [(i, j) for i, j in _neighbors(lines) if _disjoint_gap(lines, owners, i, j)]
    incoming = Counter(j for _, j in candidates)
    links = {i: j for i, j in candidates if incoming[j] == 1}
    if not any(i + 1 < j for i, j in links.items()):
        return None

    followers = set(links.values())
    result = []
    # Every link goes forward in the original stream. Each chain is emitted
    # at its head, and unrelated chains keep their relative head order.
    for i in range(len(lines)):
        if i in followers:
            continue
        while True:
            result.append(ordered[i])
            if i not in links:
                break
            i = links[i]
    return result
