"""Shared accounting for images and image regions read by OCR: ink, masking and limits.

Used by the PDF route (image regions on text-layer pages, and whole pages) and by the DOCX route
(embedded images). Every threshold is a named constant. They are provisional, tuned on
D-bench synthetic data and the dev split only.
"""

from __future__ import annotations

RENDER_DPI = 300
# Rendering is capped at this many pixels (heocr-engine uses 40 M); the dpi drops to fit.
MAX_RENDER_PIXELS = 40_000_000
# After masking the layer's characters, a region needs this much ink (grey level below INK_LEVEL)
# to be worth OCR: at least MIN_INK_PIXELS, and MIN_INK_SHARE of its area.
INK_LEVEL = 160
MIN_INK_PIXELS = 50
MIN_INK_SHARE = 0.0005
# Within a page sent to region OCR, regions under this share of the page are counted, not recognized.
REGION_MIN_PAGE_SHARE = 0.01
# OCR is "short for its ink" when at least this many dark pixels, and this share of all dark
# pixels, lie outside every line it read (provisional; tuned on D-bench and dev only).
SHORT_INK_MIN_PX = 5000
SHORT_INK_SHARE = 0.5
# Image regions smaller than this (in rendered pixels, either side) are icons, not text.
MIN_REGION_PX = 24
# Region OCR below this mean word confidence is stray marks from logos and drawings (dev split:
# below 50 the results were single characters and punctuation; from 80 up, real labels).
REGION_MIN_CONFIDENCE = 50.0


def paper_level(image) -> int:
    """The paper's grey level: the most common level in the lighter half of the image.

    Masks painted before OCR use it, never pure white: on a scan whose paper is grey, a white
    patch is the lightest thing in the image, and a global threshold (Tesseract's Otsu) then puts
    the whole paper on the ink side, and the text around the patch disappears with it.
    """
    histogram = image.convert("L").histogram()
    if not any(histogram[128:]):
        return 255  # no paper to match (a dark photo, a solid fill): white, as before
    return max(range(128, 256), key=lambda v: histogram[v])


def mask_layer_text(crop, origin: tuple[float, float], scale: float, boxes) -> tuple[object, int]:
    """Paint the text layer's character boxes in the paper's grey inside an image crop.

    ``origin`` is the crop's top-left corner in page points; ``boxes`` are character boxes in
    points. Returns the masked image and the number of characters masked.
    """
    from PIL import ImageDraw

    image = crop.convert("L")
    paper = paper_level(image)
    draw = ImageDraw.Draw(image)
    width, height = image.size
    masked = 0
    for x0, y0, x1, y1 in boxes:
        left, top = (x0 - origin[0]) * scale - 1, (y0 - origin[1]) * scale - 1
        right, bottom = (x1 - origin[0]) * scale + 1, (y1 - origin[1]) * scale + 1
        if right <= 0 or bottom <= 0 or left >= width or top >= height:
            continue
        draw.rectangle([left, top, right, bottom], fill=paper)
        masked += 1
    return image, masked


def has_ink(image) -> bool:
    """Whether a grey image holds anything darker than paper, beyond a few stray pixels."""
    histogram = image.histogram()
    dark = sum(histogram[:INK_LEVEL])
    return dark >= max(MIN_INK_PIXELS, MIN_INK_SHARE * image.size[0] * image.size[1])


def ink_outside(image, boxes_px, pad: int = 2) -> tuple[int, int]:
    """Dark pixels in a grey image, in total and outside the given boxes (OCR line boxes)."""
    from PIL import ImageDraw

    total = sum(image.histogram()[:INK_LEVEL])
    if not total:
        return 0, 0
    rest = image.copy()
    draw = ImageDraw.Draw(rest)
    for x0, y0, x1, y1 in boxes_px:
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=255)  # counting only: never sent to OCR
    return total, sum(rest.histogram()[:INK_LEVEL])


def dense_ink_outside(image, boxes_px, pad: int, tile_px: int, density: float) -> tuple[int, int]:
    """Dark pixels in a grey image, in total and outside the given boxes **in dense areas only**:
    tiles of ``tile_px`` whose uncovered ink is at least ``density`` of the tile. Text left
    uncovered fills its tiles; rulings (straight or skewed) and specks fill a few percent of any
    tile, and are not counted."""
    from PIL import Image, ImageDraw

    gray = image.convert("L")
    total = sum(gray.histogram()[:INK_LEVEL])
    if not total:
        return 0, 0
    rest = gray.point(lambda v: 0 if v < INK_LEVEL else 255)
    draw = ImageDraw.Draw(rest)
    for x0, y0, x1, y1 in boxes_px:
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=255)
    w, h = rest.size
    tiles = rest.resize((max(1, w // tile_px), max(1, h // tile_px)), Image.BOX)
    dense = tiles.point(lambda v: 255 if v <= 255 * (1 - density) else 0)  # mean white at most 1 - density
    mask = dense.resize((w, h), Image.NEAREST)
    kept = Image.new("L", (w, h), 255)
    kept.paste(rest, (0, 0), mask)
    return total, sum(kept.histogram()[:INK_LEVEL])


def short_for_ink(image, result) -> tuple[bool, float]:
    """Whether OCR left much of an image's ink outside every line it read (region accounting)."""
    total, outside = ink_outside(image, [line.bbox for line in result.lines])
    share = outside / total if total else 0.0
    return outside >= SHORT_INK_MIN_PX and share >= SHORT_INK_SHARE, round(share, 4)
