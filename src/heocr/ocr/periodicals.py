"""Reading order of article regions supported by OCR line geometry."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
import re
from statistics import median

from heocr.ocr.tesseract import OcrLine

_PAGE_NUMBER = re.compile(r"\s*[-–—·(\[]?\s*\d{1,4}\s*[-–—·)\]]?\s*")


@dataclass
class Region:
    lines: list[OcrLine]
    role: str = "body"
    heading: bool = False
    article: int | None = None
    columns: tuple[int, ...] = ()


@dataclass
class PageLayout:
    regions: list[Region]
    columns: int


@dataclass
class _Article:
    headings: list[tuple[int, OcrLine]] = field(default_factory=list)
    lanes: tuple[int, ...] = ()
    lines: dict[int, list[OcrLine]] = field(default_factory=lambda: defaultdict(list))


def _height(line: OcrLine) -> float:
    return line.bbox[3] - line.bbox[1]


def _y(line: OcrLine) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2


def _vertical_overlap(a: list[OcrLine], b: list[OcrLine]) -> float:
    def extent(lines):
        return min(line.bbox[1] for line in lines), max(line.bbox[3] for line in lines)

    a0, a1 = extent(a)
    b0, b1 = extent(b)
    return min(a1, b1) - max(a0, b0)


def _lanes(body: list[OcrLine], width: int, height: float):
    """Recover gaps in the body lanes. An oversized heading is no gutter vote."""
    cuts = []
    run = []
    pad = max(width * .002, height * .20)
    for step in range(56, 345):
        x = step * width / 400
        left = [line for line in body if line.bbox[2] <= x - pad]
        right = [line for line in body if line.bbox[0] >= x + pad]
        if (min(len(left), len(right)) >= 5 and len(left) + len(right) >= len(body) - 1
                and _vertical_overlap(left, right) >= 6 * height):
            run.append(x)
        elif run:
            cuts.append(median(run))
            run = []
    if run:
        cuts.append(median(run))
    if not 1 <= len(cuts) <= 5:
        return None
    bands = [[] for _ in range(len(cuts) + 1)]
    for line in body:
        a, b = bisect_right(cuts, line.bbox[0]), bisect_right(cuts, line.bbox[2])
        if a == b:
            bands[a].append(line)
    if (any(len(band) < 5 for band in bands)
            or any(_vertical_overlap(a, b) < 6 * height for a, b in zip(bands, bands[1:]))):
        return None
    bounds = [(min(line.bbox[0] for line in band), max(line.bbox[2] for line in band)) for band in bands]
    return cuts, bands, bounds


def _furniture(lines: list[OcrLine], page_height: int, height: float, start: float, end: float):
    headers, footers = [], []
    for line in lines:
        _, y0, _, y1 = line.bbox
        words = line.text.split()
        number = _PAGE_NUMBER.fullmatch(line.text)
        if _height(line) > 1.25 * height or len(words) > 7:
            continue
        if y1 <= .10 * page_height and y1 + 1.45 * height <= start and (number or len(words) >= 2):
            headers.append(line)
        elif y0 >= .90 * page_height and y0 >= end + 1.45 * height and (number or len(words) >= 2):
            footers.append(line)
    return headers, footers


def _headlines(lines, bands, bounds, height):
    proposed = []
    for i, line in sorted(enumerate(lines), key=lambda item: _y(item[1])):
        x0, y0, x1, _ = line.bbox
        if _height(line) < 1.44 * height or len(line.text.split()) > 13 or not any(ch.isalpha() for ch in line.text):
            continue
        lanes = tuple(lane for lane, (a, b) in enumerate(bounds) if min(b, x1) - max(a, x0) >= .18 * (b - a))
        if not lanes or lanes != tuple(range(lanes[0], lanes[-1] + 1)):
            continue
        previous = next((article for article in reversed(proposed) if article.lanes == lanes), None)
        if previous is not None:
            a, _, b, bottom = previous.headings[-1][1].bbox
            if 0 <= y0 - bottom <= .9 * height and min(x1, b) - max(x0, a) >= .5 * min(x1 - x0, b - a):
                previous.headings.append((i, line))
                continue
        proposed.append(_Article([(i, line)], lanes))
    found = {}
    for article in proposed:
        y1 = article.headings[-1][1].bbox[3]
        # A spanning title must lead into body columns starting at a comparable height.
        starts = [min((row.bbox[1] for row in bands[lane] if row.bbox[1] >= y1 - .2 * height), default=float("inf"))
                  for lane in article.lanes]
        if max(starts) <= y1 + 3.1 * height and max(starts) - min(starts) <= 2.2 * height:
            found[article.headings[0][0]] = article
    return found

def _articles(lines, cuts, headings):
    # Nodes are whole articles, and each lane imposes an independent top-to-bottom
    # constraint. A spanning article finishes its other lanes before the next item.
    articles = dict(headings)
    predecessors = defaultdict(set)
    lanes = [[] for _ in range(len(cuts) + 1)]
    title_continuations = {i for article in headings.values() for i, _ in article.headings[1:]}
    for index, line in enumerate(lines):
        if index in title_continuations:
            continue
        if index in headings:
            for lane in headings[index].lanes:
                lanes[lane].append(index)
            continue
        a, b = bisect_right(cuts, line.bbox[0]), bisect_right(cuts, line.bbox[2])
        if a != b:
            return None
        lanes[a].append(index)
    for lane, events in enumerate(lanes):
        active = -lane - 1
        sequence = []
        for index in sorted(events, key=lambda i: (_y(lines[i]), i)):
            if index in headings:
                active = index
            else:
                articles.setdefault(active, _Article()).lines[lane].append(lines[index])
            if not sequence or active != sequence[-1]:
                sequence.append(active)
        for previous, following in zip(sequence, sequence[1:]):
            predecessors[following].add(previous)
    if any(set(article.lines) != set(article.lanes) for article in headings.values()):
        return None
    order = TopologicalSorter({key: predecessors[key] for key in articles})
    try:
        order.prepare()
    except CycleError:
        return None

    def priority(key):
        article = articles[key]
        first = article.headings[0][1] if article.headings else next(iter(article.lines.values()))[0]
        return -max(article.lines), _y(first), key

    available = list(order.get_ready())
    regions = []
    article_number = 0
    while available:
        key = min(available, key=priority)
        available.remove(key)
        article = articles[key]
        article_number += 1
        if article.headings:
            regions.append(Region([line for _, line in article.headings], heading=True,
                                  article=article_number, columns=article.lanes))
        regions.extend(Region(article.lines[lane], article=article_number, columns=(lane,))
                       for lane in sorted(article.lines, reverse=True))
        order.done(key)
        available.extend(order.get_ready())
    return regions


def plan_page(lines: list[OcrLine], size: tuple[int, int]) -> PageLayout | None:
    """Return a lossless regional partition for a corroborated RTL periodical page."""
    width, page_height = size
    if len(lines) < 12 or any(_height(line) <= 0 or line.bbox[0] >= line.bbox[2] for line in lines):
        return None
    long = [line for line in lines if line.bbox[2] - line.bbox[0] >= width * .13
            and len(line.text.split()) >= 3]
    if len(long) < 10:
        return None
    height = median(_height(line) for line in long)
    body = [line for line in long if .67 * height <= _height(line) <= 1.32 * height]
    layout = _lanes(body, width, height)
    if layout is None:
        return None
    cuts, bands, bounds = layout
    interior = [line for line in body if .10 * page_height <= _y(line) <= .90 * page_height]
    if not interior:
        return None
    headers, footers = _furniture(lines, page_height, height, min(line.bbox[1] for line in interior),
                                  max(line.bbox[3] for line in interior))
    furniture = {id(line) for line in [*headers, *footers]}
    content = [line for line in lines if id(line) not in furniture]
    headings = _headlines(content, bands, bounds, height)
    if not headings:
        return None
    regions = _articles(content, cuts, headings)
    if regions is None:
        return None

    def margin_regions(items, role):
        return [Region([line], role=role) for line in sorted(items, key=lambda line: (_y(line), -line.bbox[2]))]

    return PageLayout([*margin_regions(headers, "header"), *regions, *margin_regions(footers, "footer")],
                      len(bands))


def mark_running(doc) -> None:
    """Corroborate repeating OCR margin text on pages with a periodical partition."""
    pages = {page.index: page for page in doc.pages if "ocr_periodical_columns" in page.signals}
    if len(pages) < 2:
        return
    candidates = defaultdict(list)
    for block in doc.blocks:
        page = pages.get(block.page)
        if (page is None or block.source != "ocr" or block.kind != "text" or block.role not in {"body", "header", "footer"}
                or not block.bbox or not block.lines or len(block.lines) > 2):
            continue
        x0, y0, x1, y1 = block.bbox
        if y1 - y0 > .045 * page.height:
            continue
        role = "header" if y1 <= .11 * page.height else "footer" if y0 >= .89 * page.height else None
        key = "".join(ch for ch in block.text.casefold() if ch.isalpha())
        if role is not None and len(key) >= 8:
            candidates[role, key].append((block, (x0 + x1) / (2 * page.width), (y0 + y1) / (2 * page.height)))
    changed = False
    for (role, _), items in candidates.items():
        for block, x, y in items:
            if any(other.page != block.page and abs(other.page - block.page) <= 4
                   and abs(x - ox) <= .08 and abs(y - oy) <= .02 for other, ox, oy in items):
                block.role = role
                block.flags.append("ocr_periodical_running_furniture")
                if block.ocr is not None:
                    block.ocr.pop("periodical_article", None)
                    block.ocr.pop("periodical_columns", None)
                changed = True
    if changed:
        from heocr.pdf.furniture import _ordered
        doc.blocks[:] = _ordered(doc.blocks)
