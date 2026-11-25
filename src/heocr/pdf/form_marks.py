"""Read printed X marks in scan OCR word boxes for the form export."""
from itertools import accumulate
from math import ceil, floor
import re

from PIL import Image

from heocr.contract import time_up
from heocr.ocr.regions import paper_level

_TOKEN = re.compile(r"(?<!\S)(?:\(\s*[xXא()]?\s*\)|\[\s*[xXא\[\]]?\s*\])(?!\S)")
_LITERAL = re.compile(r"\(\s*[xX]?\s*\)|\[\s*[xX]?\s*\]")


def _vertical_runs(mask):
    columns = mask.resize((mask.width, 1), Image.Resampling.BOX).tobytes()
    return [match.span() for match in re.finditer(rb"[^\x00]+", columns)]


def _frame_side(side, *, flip=False):
    """The ends of a bracket reach towards the mark, past its middle edge."""
    if flip:
        side = side.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    box = side.getbbox()
    if box is None:
        return None
    a, b, c, d = box
    height = d - b
    if not .09 * height <= c - a <= .40 * height:
        return None
    third = max(1, height // 4)
    middle = side.crop((a, b + third, c, d - third)).getbbox()
    if not middle:
        return None
    for top, bottom in ((b, b + third), (d - third, d)):
        end = side.crop((a, top, c, bottom)).getbbox()
        if not end or end[2] - middle[2] < max(1, .10 * (c - a)):
            return None
    return box


def _cross(mark, height):
    box = mark.getbbox()
    if not box:
        return False
    a, b, c, d = box
    if not (.40 * height <= c - a <= .95 * height and .55 * height <= d - b <= 1.02 * height):
        return False
    square = mark.crop(box).resize((24, 24), Image.Resampling.BOX)

    def has_ink(x, y, radius=2):
        return square.crop((x - radius, y - radius, x + radius + 1, y + radius + 1)).getextrema()[1] >= 160

    return (all(has_ink(x, y) for x, y in ((4, 4), (19, 4), (11, 11), (4, 19), (19, 19)))
            and not any(has_ink(x, y, 1) for x, y in ((11, 2), (4, 11), (19, 11), (11, 21))))


def _printed_x(image, words):
    left, top, right, bottom = (bound(word.bbox[i] for word in words)
                                for i, bound in enumerate((min, min, max, max)))
    width, height = right - left, bottom - top
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        return False
    if height < 12 or not .65 * height <= width <= 1.9 * height or max(width, height) > 180:
        return False
    pad = min(3, round(height / 16))
    patch = image.crop((max(0, floor(left) - pad), max(0, floor(top) - pad),
                        min(image.width, ceil(right) + pad), min(image.height, ceil(bottom) + pad))).convert("L")
    if patch.width < 12 or patch.height < 12:
        return False
    paper = paper_level(patch)
    # High-contrast and stricter masks can separate sides nearly touching serifs.
    for cutoff in (paper - 85, paper - 125):
        mask = patch.point(lambda gray: 255 if gray < cutoff else 0)
        runs = _vertical_runs(mask)
        if len(runs) != 3:
            continue
        sides = [mask.crop((a, 0, b, mask.height)) for a, b in (runs[0], runs[2])]
        first = _frame_side(sides[0])
        last = _frame_side(sides[1], flip=True)
        if not first or not last:
            continue
        height = max(first[3], last[3]) - min(first[1], last[1])
        if max(abs(first[1] - last[1]), abs(first[3] - last[3])) > .15 * height:
            continue
        a, b = runs[1]
        if _cross(mask.crop((a, 0, b, mask.height)), height):
            return True
    return False


def _repair_mark_reading(image, reading, text):
    """Return a forms-only alternate reading when framed X ink corroborates it."""
    words = reading.words
    if not words or text != " ".join(word.text for word in words):
        return None
    marks = list(_TOKEN.finditer(text))
    if not marks:
        return None
    if len(marks) == 1:
        mark = marks[0]
        before, after = text[:mark.start()].strip(), text[mark.end():].strip()
        if (bool(before) == bool(after) or ":" in after
                or (":" in before and not (before.endswith(":") and before.count(":") == 1))):
            return None
    elif not text[:marks[0].start()].rstrip().endswith(":"):
        return None
    starts = [0, *accumulate(len(word.text) + 1 for word in words)]
    updates = []
    for mark in marks:
        token = mark[0]
        if _LITERAL.fullmatch(token) and "x" in token.casefold():
            continue
        members = [word for i, word in enumerate(words)
                   if starts[i] >= mark.start() and starts[i + 1] - 1 <= mark.end()]
        if members and _printed_x(image, members):
            updates.append((mark, token[0] + "X" + token[-1]))
        elif not _LITERAL.fullmatch(token):
            return None
    if not updates:
        return None
    for match, replacement in reversed(updates):
        text = text[:match.start()] + replacement + text[match.end():]
    return text


def collect(doc, blocks, image, readings):
    """Attach pixel evidence while OCR word boxes still use this image's coordinates."""
    for block in blocks:
        for line in block.lines or ():
            if time_up(doc):
                return
            reading = readings.get(id(line))
            if reading is not None and (alternate := _repair_mark_reading(image, reading, line.text)):
                line._form_mark_reading = (line.text, alternate)
