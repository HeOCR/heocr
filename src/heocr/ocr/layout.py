"""Evidence-gated layout refinements for whole-page OCR and ruled cells."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import replace
import re
from statistics import median

from heocr.ocr.regions import paper_level
from heocr.ocr.tesseract import OcrResult, OcrUnavailable

_HEBREW = re.compile(r"[א-ת]")
_LTR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@,+%−-]*")
_CONNECTOR = re.compile(r"[/.:,−-]")
_NUMBER = re.compile(r"\d+(?:[.,:/−–-]\d+)*")


def normalize_ltr(lines) -> int:
    """Keep Hebrew traversal and restore left-to-right order within bounded mixed runs."""
    changed = 0
    for line in lines:
        words = line.words
        if len(words) < 4 or line.text != " ".join(word.text for word in words):
            continue
        start = 1
        while start < len(words) - 1:
            before = words[start - 1]
            if not _HEBREW.search(before.text) or not _LTR.fullmatch(words[start].text):
                start += 1
                continue
            stop = start + 1
            while stop < len(words) and (_LTR.fullmatch(words[stop].text) or _CONNECTOR.fullmatch(words[stop].text)):
                stop += 1
            if stop == len(words):
                break
            run = words[start:stop]
            after = words[stop]
            if len(run) >= 2 and _HEBREW.search(after.text) and _LTR.fullmatch(run[-1].text):
                ordered = sorted(run, key=lambda word: word.bbox[0])
                height = min(word.bbox[3] - word.bbox[1] for word in run)
                unambiguous = (any(re.search(r"[A-Za-z]", word.text) for word in run)
                               or any(_CONNECTOR.fullmatch(word.text) for word in run))
                if (unambiguous and height > 0 and ordered != run
                        and after.bbox[2] <= ordered[0].bbox[0] <= ordered[-1].bbox[2] <= before.bbox[0]
                        and all(0 <= b.bbox[0] - a.bbox[2] <= height
                                and min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1]) >= height / 2
                                for a, b in zip(ordered, ordered[1:]))):
                    words[start:stop] = ordered
                    line.text = " ".join(word.text for word in words)
                    line.confidences[:] = [word.confidence for word in words]
                    changed += 1
            start = stop
    return changed


def _margin_order(lines, prose, page_width, height):
    """Interleave a recurring narrow left note lane with its adjacent RTL prose rows."""
    body_lines = [line for line in prose if line.bbox[2] - line.bbox[0] >= page_width * .30]
    if len(body_lines) < 12:
        return None
    edge = min(line.bbox[0] for line in body_lines)
    if not page_width * .16 <= edge <= page_width * .48:
        return None
    margin = [line for line in lines if line.bbox[2] <= edge - height / 2]
    body = sorted((line for line in lines if line.bbox[0] >= edge), key=lambda line: line.bbox[1])
    if len(margin) < 4 or len(body) + len(margin) != len(lines):
        return None
    if any(line.bbox[2] - line.bbox[0] > .15 * page_width or len(line.text.split()) > 4
           or len(_HEBREW.findall(line.text)) < 3 for line in margin):
        return None
    by_row = {}
    for line in sorted(margin, key=lambda line: line.bbox[1]):
        middle = (line.bbox[1] + line.bbox[3]) / 2
        difference, index = min((abs(middle - (row.bbox[1] + row.bbox[3]) / 2), i)
                                for i, row in enumerate(body))
        if difference > height or index in by_row:
            return None
        by_row[index] = line
    if list(by_row) != sorted(by_row):
        return None
    ordered = []
    for i, row in enumerate(body):
        ordered.append(row)
        if i in by_row:
            note = by_row[i]
            ordered.append(replace(note, paragraph=(f"margin:{i}", note.paragraph[1])))
    return ordered if [line.bbox for line in ordered] != [line.bbox for line in lines] else None


def _band_columns(body, prose, cuts, height):
    bands = [[] for _ in range(len(cuts) + 1)]
    for line in body:
        bands[bisect_right(cuts, line.bbox[0])].append(line)
    boxes = []
    for band in bands:
        support = [line.bbox for line in band if id(line) in prose]
        if len(support) < 4:
            return None
        boxes.append((min(box[1] for box in support), max(box[3] for box in support)))
    if any(min(a[1], b[1]) - max(a[0], b[0]) < 5 * height for a, b in zip(boxes, boxes[1:])):
        return None
    return bands[::-1]


def column_order(lines, page_width: int) -> list | None:
    """Read sustained columns right first, retaining spanning headings and footers."""
    if len(lines) < 8:
        return None
    height = median(line.bbox[3] - line.bbox[1] for line in lines)
    if height <= 0:
        return None
    gutter = max(2 * height, page_width * .035)
    prose = [line for line in lines if line.bbox[2] - line.bbox[0] >= page_width * .14
             and len(line.text.split()) >= 3]
    if len(prose) < 8:
        return None
    cuts = {(line.bbox[2] + gutter / 2) for line in prose} | {(line.bbox[0] - gutter / 2) for line in prose}
    picks = []
    for cut in sorted(cuts):
        if not page_width * .25 <= cut <= page_width * .75:
            continue
        left_edge, right_edge = cut - gutter / 2, cut + gutter / 2
        left = [line for line in prose if line.bbox[2] <= left_edge]
        right = [line for line in prose if line.bbox[0] >= right_edge]
        if min(len(left), len(right)) < 4:
            continue
        start = max(min(line.bbox[1] for line in side) for side in (left, right))
        end = min(max(line.bbox[3] for line in side) for side in (left, right))
        if end - start < 5 * height:
            continue
        sides = [[], [], [], []]
        for line in lines:
            x0, y0, x1, y1 = line.bbox
            if y1 <= start and x0 < right_edge and x1 > left_edge:
                sides[0].append(line)
            elif y0 >= end and x0 < right_edge and x1 > left_edge:
                sides[3].append(line)
            elif x0 >= right_edge:
                sides[1].append(line)
            elif x1 <= left_edge:
                sides[2].append(line)
            else:
                break  # an interleaved spanning line makes the two flows ambiguous
        else:
            body = sides[1] + sides[2]
            if any(line.bbox[3] > min(item.bbox[1] for item in body) for line in sides[0]):
                continue
            if any(line.bbox[1] < max(item.bbox[3] for item in body) for line in sides[3]):
                continue
            rank = min(len(left), len(right)), -abs(cut - page_width / 2)
            picks.append((rank, cut, sides))
    if not picks:
        return _margin_order(lines, prose, page_width, height)
    picks.sort(key=lambda pick: pick[0], reverse=True)
    _, best_cut, (header, right, left, footer) = picks[0]
    body = right + left
    bounds = [best_cut]
    bands = [right, left]
    supported = {id(line) for line in prose}
    for _, cut, sides in picks[1:]:
        if sides[0] != header or sides[3] != footer:
            continue
        next_bounds = sorted([*bounds, cut])
        refined = _band_columns(body, supported, next_bounds, height)
        if refined is not None:
            bounds, bands = next_bounds, refined
    grouped = (header, *bands, footer)
    if [id(line) for side in grouped for line in side] == [id(line) for line in lines]:
        return None  # keep the existing intra-column order when traversal is already grouped
    return [replace(line, paragraph=(f"{band}:{line.paragraph[0]}", line.paragraph[1]))
            for band, side in enumerate(grouped) for line in sorted(side, key=lambda item: item.bbox[1])]


def _edge_ink(image):
    w, h = image.size
    if min(w, h) < 6:
        return ()
    strips = ((0, 0, 2, h), (0, 0, w, 2), (w - 2, 0, w, h), (0, h - 2, w, h))
    edges = []
    # A black edge extending along the whole scan is a border, not evidence of cropped text.
    for index, box in enumerate(strips):
        crop = image.crop(box)
        if crop.mode != "L":
            crop = crop.convert("L")
        dark = sum(crop.histogram()[:128])
        if 4 <= dark < crop.width * crop.height / 3:
            edges.append(index)
    return edges


def _touches(box, edges, size, reach):
    x0, y0, x1, y1 = box
    distances = x0, y0, size[0] - x1, size[1] - y1
    return any(distances[edge] <= reach for edge in edges)


def _shift(result, padding, size):
    def box(values):
        return [max(0, min(size[index % 2], value - padding)) for index, value in enumerate(values)]
    return OcrResult([
        replace(line, bbox=box(line.bbox), words=[replace(word, bbox=box(word.bbox)) for word in line.words])
        for line in result.lines
    ])


def _preserves(primary, candidate, edges, size, padding):
    """Require exact existing text or an extension of an edge line. Keep existing numeric runs."""
    before = [" ".join(line.text.split()) for line in primary.lines]
    after = [" ".join(line.text.split()) for line in candidate.lines]
    if before == after or (candidate.mean_confidence or 0) < 80:
        return False
    if _NUMBER.findall(" ".join(before)) != _NUMBER.findall(" ".join(after)):
        return False
    index = 0
    for line, text in zip(candidate.lines, after, strict=True):
        if index < len(before) and text == before[index]:
            index += 1
            continue
        if not _touches(line.bbox, edges, size, padding) or not any(c.isalnum() for c in text):
            return False
        if index < len(before) and before[index] in text:
            if not _touches(primary.lines[index].bbox, edges, size, padding):
                return False
            index += 1
    return index == len(before)


def clip_guard(engine, image, dpi, primary, *, psm=None):
    """Retry with paper padding only when visible ink meets the image or crop edge."""
    from PIL import ImageOps

    edges = _edge_ink(image)
    if not edges:
        return None
    gray = image if image.mode == "L" else image.convert("L")
    padding = max(6, round(dpi / 24))
    padded = ImageOps.expand(gray, border=padding, fill=paper_level(gray))
    try:
        candidate = _shift(engine.recognize(padded, dpi=dpi, psm=psm), padding, gray.size)
    except OcrUnavailable:
        return None
    return candidate if _preserves(primary, candidate, edges, gray.size, padding) else None
