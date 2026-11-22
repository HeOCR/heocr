"""Bridge short horizontal dropouts across otherwise connected printed strokes."""

from statistics import median

from PIL import Image, ImageChops

_INK = 155
_RADIUS = 2  # Maximum gap of four pixels at the 300-DPI PDF OCR raster.
_MIN_PIXELS = 28
_MIN_GROUPS = 5


def _vertical_extreme(image, extreme, fill):
    result = image
    for offset in (-_RADIUS, -1, 1, _RADIUS):
        shifted = Image.new("L", image.size, fill)
        shifted.paste(image, (0, offset))
        result = extreme(result, shifted)
    return result


def _streak_rows(added, ink):
    """Find narrow, repeated bands with bridges across several separated strokes."""
    width, height = added.size
    # The inexpensive row means only screen candidates. Use exact counts for decisions.
    approximate = added.resize((1, height), Image.Resampling.BOX).tobytes()
    at_least = max(1, _MIN_PIXELS * 255 // width)
    bands = []
    for row in range(_RADIUS, height - _RADIUS):
        if approximate[row] < at_least:
            continue
        box = (0, row, width, row + 1)
        count = added.crop(box).tobytes().count(b"\xff")
        if count < _MIN_PIXELS:
            continue
        total = count + ink.crop(box).tobytes().count(b"\xff")
        if count * 3 < total:
            continue
        if not bands or row - bands[-1][-1][0] > 2 * _RADIUS + 1:
            bands.append([])
        bands[-1].append((row, count, total))

    confirmed = []
    for band in bands:
        if band[-1][0] - band[0][0] > 2 * _RADIUS:
            continue
        peak, count, total = max(band, key=lambda record: record[1])
        if count * 2 < total:
            continue
        support = [i for i, value in enumerate(added.crop((0, peak, width, peak + 1)).tobytes()) if value]
        if (support[-1] - support[0]) * 8 < width:
            continue
        groups = 1 + sum(right - left > 7 for left, right in zip(support, support[1:]))
        if groups >= _MIN_GROUPS:
            confirmed.append(band)
    return confirmed


def bridge_scan_stroke_gaps(image):
    """Return an OCR working raster and a count of repaired dropout bands.

    A vertical closing of dark strokes supplies possible bridges. Restrict it to
    the missing pixels in corroborated bands. A single band can occur in intact
    print, so require separate bands before changing the working raster.
    """
    if image.width < 1000 or image.height < 1000:
        return image, 0
    gray = image.convert("L")
    # With dark ink, grayscale opening closes thin white gaps.
    closed = _vertical_extreme(_vertical_extreme(gray, ImageChops.darker, 255), ImageChops.lighter, 0)
    ink = gray.point(lambda value: 255 if value < _INK else 0)
    added = ImageChops.subtract(closed.point(lambda value: 255 if value < _INK else 0), ink)
    bands = _streak_rows(added, ink)
    if len(bands) < 2:
        return image, 0
    result = image.copy()
    for band in bands:
        for row, _, _ in band:
            box = (0, row, image.width, row + 1)
            result.paste(closed.crop(box), (0, row), added.crop(box))
    return result, len(bands)


def has_prose_column(lines, width):
    """Require several separate long lines sharing an RTL prose margin."""
    eligible = [line.bbox for line in lines if line.bbox[2] - line.bbox[0] >= width / 2 and len(line.words) >= 6]
    if len(eligible) < 6:
        return False
    margin = median(box[2] for box in eligible)
    bottom = -1
    count = 0
    for _, top, right, end in sorted(eligible, key=lambda box: box[1]):
        if abs(right - margin) <= width * .04 and top >= bottom:
            count += 1
            bottom = end
    return count >= 6
