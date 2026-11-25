"""Confirm Latin words from the English page pass on mixed Hebrew OCR lines."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor
import re
from statistics import median

from heocr.ocr.tesseract import OcrUnavailable, OcrWord

LATIN_MIN_CONFIDENCE = 80
LATIN_LINES_PER_PAGE = 8
WORD_OVERLAP = 0.8
LATIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'._/@:+-]*")
EDGE_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._@/+:-]*[-_/][A-Za-z0-9._@/+:-]+")
HEBREW = re.compile(r"[א-ת]")
HEBREW_WORD = re.compile(r"[א-ת]+")
BIDI_FORMAT = str.maketrans("", "", "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _latin(text):
    return LATIN_TOKEN.fullmatch(text) and sum(ch.isalpha() for ch in text) >= 3


def _covers(box, target):
    x0, y0, x1, y1 = target
    return (min(box[2], x1) - max(box[0], x0) >= WORD_OVERLAP * (x1 - x0) > 0
            and min(box[3], y1) - max(box[1], y0) >= WORD_OVERLAP * (y1 - y0) > 0)


def _union(words):
    return [min(w.bbox[0] for w in words), min(w.bbox[1] for w in words),
            max(w.bbox[2] for w in words), max(w.bbox[3] for w in words)]


@dataclass(frozen=True)
class Replacement:
    start: int
    stop: int
    word: OcrWord


def _proposals(line, english):
    if not HEBREW.search(line.text):
        return []
    out = []
    for word in english:
        if not _covers(line.bbox, word.bbox):
            continue
        indexes = [i for i, primary in enumerate(line.words) if _covers(word.bbox, primary.bbox)]
        if not indexes or len(indexes) != indexes[-1] - indexes[0] + 1:
            continue
        start, stop = indexes[0], indexes[-1] + 1
        original = line.words[start:stop]
        if not _covers(_union(original), word.bbox):
            continue
        if not any(HEBREW.search(w.text) for w in line.words[:start] + line.words[stop:]):
            continue  # require surrounding Hebrew outside the proposed Latin replacement
        out.append(Replacement(start, stop, word))
    out.sort(key=lambda item: (item.start, item.stop))
    return [item for i, item in enumerate(out)
            if (i == 0 or out[i - 1].stop <= item.start) and (i + 1 == len(out) or item.stop <= out[i + 1].start)]


def _protected(text):
    marks = "()[]{}%₪$€£"
    if any(ch.isdigit() for ch in text):
        marks += ".,:/+-−–"
    return "".join(ch for ch in text if ch.isdigit() or ch in marks)


def _replaceable(original, replacement):
    if any(HEBREW_WORD.fullmatch(w.text) and w.confidence >= LATIN_MIN_CONFIDENCE for w in original):
        return False
    return _protected("".join(w.text for w in original)) == _protected(replacement)


def _confirmed(image, dpi, line, proposals, reviewer):
    pad = 3
    x0, y0, x1, y1 = line.bbox
    crop = (max(0, floor(x0) - pad), max(0, floor(y0) - pad),
            min(image.width, ceil(x1) + pad), min(image.height, ceil(y1) + pad))
    if crop[2] <= crop[0] or crop[3] <= crop[1]:
        return []
    try:
        reading = reviewer.recognize(image.crop(crop), dpi=dpi, psm=7)
    except OcrUnavailable:
        return []
    if not any(HEBREW.search(line.text) for line in reading.lines):
        return []
    by_text = {}
    for line in reading.lines:
        for word in line.words:
            if word.confidence >= LATIN_MIN_CONFIDENCE:
                x0, y0, x1, y1 = word.bbox
                box = [x0 + crop[0], y0 + crop[1], x1 + crop[0], y1 + crop[1]]
                by_text.setdefault(word.text.translate(BIDI_FORMAT), []).append(box)
    return [item for item in proposals
            if any(_covers(box, item.word.bbox) and _covers(item.word.bbox, box) for box in by_text.get(item.word.text, ()))]


def _joins(left, right):
    a, b = sorted((left.bbox, right.bbox), key=lambda box: box[0])
    return b[0] - a[2] <= min(a[3] - a[1], b[3] - b[1])


def _runs(proposals):
    runs = []
    for item in proposals:
        if runs and runs[-1][-1].stop == item.start and _joins(runs[-1][-1].word, item.word):
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def _apply(line, runs, accepted):
    confirmed = {item.start for item in accepted}
    changed = 0
    for run in reversed(runs):
        if any(item.start not in confirmed for item in run):
            continue
        words = sorted((item.word for item in run), key=lambda word: word.bbox[0])
        replacement = OcrWord(" ".join(w.text for w in words), _union(words), min(w.confidence for w in words))
        start, stop = run[0].start, run[-1].stop
        first = " ".join(w.text for w in line.words[start:stop])
        if first == replacement.text or _protected(first) != _protected(replacement.text):
            continue
        line.words[start:stop] = [replacement]
        changed += 1
    if changed:
        line.text = " ".join(w.text for w in line.words)
        line.latin_runs_reread += changed


def reread_latin_runs(image, dpi, lines, english, english_size, get_reviewer):
    """Use English word candidates only with closely aligned mixed-language confirmation."""
    sx, sy = image.width / english_size[0], image.height / english_size[1]
    candidates = []
    for line in english.lines:
        for word in line.words:
            text = word.text.translate(BIDI_FORMAT)
            if word.confidence >= LATIN_MIN_CONFIDENCE and _latin(text):
                x0, y0, x1, y1 = word.bbox
                candidates.append(OcrWord(text, [x0 * sx, y0 * sy, x1 * sx, y1 * sy], word.confidence))
    if not candidates:
        return
    reviewed = 0
    for line in lines:
        runs = [run for run in _runs(_proposals(line, candidates))
                if all(_replaceable(line.words[item.start:item.stop], item.word.text) for item in run)]
        if not runs:
            continue
        reviewer = get_reviewer()
        if reviewer is None:
            return
        proposals = [item for run in runs for item in run]
        _apply(line, runs, _confirmed(image, dpi, line, proposals, reviewer))
        reviewed += 1
        if reviewed >= LATIN_LINES_PER_PAGE:
            break


def reread_latin_edges(image, dpi, lines, reader):
    """Use agreeing English reads of isolated identifier lines at the text edges."""
    if not 5 <= len(lines) <= 80:
        return 0
    vertical = sorted(lines, key=lambda line: line.bbox[1])
    edges = ((vertical[0], vertical[1].bbox[1] - vertical[0].bbox[3]),
             (vertical[-1], vertical[-1].bbox[1] - vertical[-2].bbox[3]))
    pad = max(2, round(.15 * median(line.bbox[3] - line.bbox[1] for line in lines)))
    slots = []
    for line, gap in edges:
        x0, y0, x1, y1 = line.bbox
        if not 1 <= len(line.words) <= 12 or gap < .65 * (y1 - y0) or x1 - x0 > .70 * image.width:
            continue
        if sum(char.isdigit() for char in line.text) < 3 or sum(char in "·.-/:" for char in line.text) < 3:
            continue
        box = (max(0, floor(x0) - 2 * pad), max(0, floor(y0) - pad),
               min(image.width, floor(x1) + 2 * pad), min(image.height, floor(y1) + pad))
        if box[0] < box[2] and box[1] < box[3]:
            slots.append((line, box))
    if not slots:
        return 0

    crops = [image.crop(box) for _, box in slots]
    try:
        first = reader.recognize_batch(crops, dpi, psm=7)
        second = reader.recognize_batch(crops, dpi, psm=13)
        if len(first) != len(slots) or len(second) != len(slots):
            raise OcrUnavailable("Latin edge reader returned an incomplete batch")
    except OcrUnavailable:
        reader.failures += 1
        return 0
    accepted = 0
    for (line, box), a, b in zip(slots, first, second, strict=True):
        if len(a.lines) != 1 or len(b.lines) != 1:
            continue
        read = a.lines[0]
        text = read.text
        if (not read.words or text == line.text or not EDGE_IDENTIFIER.search(text)
                or len(re.findall(r"[A-Za-z]", text)) < 6):
            continue
        if re.sub(r"[^A-Za-z0-9]", "", text) != re.sub(r"[^A-Za-z0-9]", "", b.lines[0].text):
            continue
        x, y = box[:2]
        words = [OcrWord(word.text, [word.bbox[0] + x, word.bbox[1] + y, word.bbox[2] + x, word.bbox[3] + y],
                         word.confidence) for word in read.words]
        line.words[:] = words
        line.confidences[:] = read.confidences
        line.bbox = _union(words)
        line.text = text
        line.latin_runs_reread += 1
        accepted += 1
    return accepted
