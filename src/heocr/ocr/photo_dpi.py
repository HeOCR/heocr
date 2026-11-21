"""Estimate a Tesseract resolution hint from glyph size in a camera image."""

from __future__ import annotations

import re

from PIL import Image, ImageChops, ImageFilter

_INK_RUN = re.compile(b"\xff+")
_MAX_RUNS = 100_000


def _component_boxes(mask):
    """Bounded eight-connected components of a binary mask, as boxes and ink area."""
    width, height = mask.size
    pixels = mask.tobytes()
    parents = []
    runs = []
    previous = []

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for y in range(height):
        current = []
        left_index = 0
        start = y * width
        for match in _INK_RUN.finditer(pixels, start, start + width):
            if len(runs) >= _MAX_RUNS:
                return ()
            left, right = match.start() - start, match.end() - start
            index = len(runs)
            parents.append(index)
            runs.append((left, right, y))
            current.append((left, right, index))
            while left_index < len(previous) and previous[left_index][1] < left:
                left_index += 1
            neighbor = left_index
            while neighbor < len(previous) and previous[neighbor][0] <= right:
                parents[root(previous[neighbor][2])] = index
                neighbor += 1
        previous = current

    boxes = {}
    for index, (left, right, y) in enumerate(runs):
        component = root(index)
        if component not in boxes:
            boxes[component] = (left, y, right, y + 1, right - left)
        else:
            x0, y0, x1, _, area = boxes[component]
            boxes[component] = min(x0, left), y0, max(x1, right), y + 1, area + right - left
    return boxes.values()


def _quantile(sorted_values, q):
    position = (len(sorted_values) - 1) * q
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (position - low) * (sorted_values[high] - sorted_values[low])


def estimate_photo_dpi(image) -> int | None:
    """Return a recognition-only hint when a camera image has enough similarly sized ink."""
    gray = image.convert("L")
    gray.thumbnail((1700, 1700), Image.Resampling.LANCZOS)
    width, height = gray.size
    # A locally blurred background lets the component filter ignore broad shadows.
    window = max(15, round(min(width, height) / 35) | 1)
    background = gray.filter(ImageFilter.GaussianBlur(.15 * window + .35))
    mask = ImageChops.subtract(background, gray).point([0] * 15 + [255] * 241)
    heights = []
    for x0, y0, x1, y1, area in _component_boxes(mask):
        w, h = x1 - x0, y1 - y0
        if (5 <= h <= .06 * height and .20 * h <= w <= 1.8 * h
                and .15 * w * h <= area <= .93 * w * h
                and .10 * width <= x0 <= x1 <= .90 * width
                and .10 * height <= y0 <= y1 <= .90 * height):
            heights.append(h)
    if len(heights) < 40:
        return None
    heights.sort()
    median = _quantile(heights, .5)
    if _quantile(heights, .75) - _quantile(heights, .25) > .55 * median:
        return None
    # Tesseract estimates image resolution with a 10x multiplier for blob height.
    return max(70, min(600, 10 * round(median * image.height / height)))
