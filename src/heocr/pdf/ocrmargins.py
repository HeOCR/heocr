"""Recover reading order for detached RTL clause numbers in whole-page OCR."""

from collections import Counter
import re

from heocr.ocr.tesseract import OcrLine

_LABEL = re.compile(r"\(?[0-9]{1,3}[.)]?")


def attach_margin_numbers(
    groups: list[list[OcrLine]], page_size: tuple[int, int],
) -> tuple[list[list[OcrLine]], set[int]]:
    """Put a margin number before its unique same-row prose line when another margin number
    supports the same x track. Keep the lines themselves, including their readings and boxes.
    """
    width, height = page_size
    margins = {}
    for i, group in enumerate(groups):
        line = group[0]
        x0, y0, x1, y1 = line.bbox
        if (len(group) == 1 and _LABEL.fullmatch(line.text.strip())
                and x0 >= 0.90 * width and 0.10 * height <= y0 < y1 <= 0.90 * height
                and 0 < x1 - x0 <= 2.5 * (y1 - y0)):
            margins[i] = line
    if len(margins) < 2:
        return groups, set()

    targets = []
    for i, group in enumerate(groups):
        if i in margins:
            continue
        for j, line in enumerate(group):
            a, b, c, d = line.bbox
            text = line.text.split()
            if (text and not _LABEL.fullmatch(text[0]) and c >= 0.72 * width and c - a >= 5 * (d - b)
                    and sum(ch.isalpha() for ch in line.text) >= 8):
                targets.append((i, j, line))

    proposals = {}
    for i, label in margins.items():
        x0, y0, _, y1 = label.bbox
        matches = []
        for group, j, line in targets:
            _, top, right, bottom = line.bbox
            line_height = bottom - top
            if (0 < x0 - right <= min(5.5 * line_height, 0.12 * width)
                    and abs(y0 + y1 - top - bottom) <= 0.6 * min(y1 - y0, line_height)):
                matches.append((group, j))
        if len(matches) == 1:
            proposals[i] = matches[0]

    counts = Counter(proposals.values())
    unique = {i: target for i, target in proposals.items() if counts[target] == 1}
    selected = set()
    for i in unique:
        x0, top, _, bottom = margins[i].bbox
        for j in unique:
            peer_x, peer_top, _, peer_bottom = margins[j].bbox
            tolerance = max(bottom - top, peer_bottom - peer_top)
            if abs(x0 - peer_x) <= tolerance and abs(top - peer_top) > 2 * tolerance:
                selected.add(i)
                break
    if not selected:
        return groups, set()

    insertions = {unique[i]: margins[i] for i in selected}
    attached = []
    for i, group in enumerate(groups):
        if i in selected:
            continue
        lines = []
        for j, line in enumerate(group):
            if (i, j) in insertions:
                lines.append(insertions[i, j])
            lines.append(line)
        attached.append(lines)
    return attached, {id(line) for line in insertions.values()}
