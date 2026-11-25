"""Verify horizontal PDF punctuation against the ink and its embedded font."""

from __future__ import annotations

import ctypes
from math import ceil, floor
from statistics import mean

from PIL import Image, ImageChops, ImageStat

from heocr.pdf.frame import frame_of
from heocr.repairs import PDF_PUNCTUATION_PAIRS

SCAN_SCALE = 3
MAX_SCAN_PIXELS = 12_000_000
INK_THRESHOLD = 96
INK = tuple(255 if level >= INK_THRESHOLD else 0 for level in range(256))


def _signature(ink):
    bounds = ink.point(INK).getbbox()
    if bounds is None:
        return None
    return ink.crop(bounds).resize((24, 36), Image.Resampling.LANCZOS)


def _opening_side(image):
    def band(start, end):
        hits = [x for y in range(start, end) for x in range(image.width)
                if image.getpixel((x, y)) >= INK_THRESHOLD]
        return mean(hits) if hits else None

    top, middle, bottom = band(2, 8), band(14, 22), band(28, 34)
    if top is None or middle is None or bottom is None:
        return None
    # Both ends of an opening bracket extend right of the middle stroke.
    if min(top, bottom) > middle + 0.5:
        return True
    if max(top, bottom) < middle - 0.5:
        return False
    return None


def _template(raw, document, font, char):
    obj = raw.FPDFPageObj_CreateTextObj(document, font, 24)
    if not obj:
        return None
    try:
        text = (raw.FPDF_WCHAR * 2)(ord(char), 0)
        if not raw.FPDFText_SetText(obj, text):
            return None
        left, bottom, right, top = (ctypes.c_float() for _ in range(4))
        if not raw.FPDFPageObj_GetBounds(obj, *(ctypes.byref(v) for v in (left, bottom, right, top))):
            return None
        if not (0 < right.value - left.value <= 72 and 0 < top.value - bottom.value <= 72):
            return None
        bitmap = raw.FPDFTextObj_GetRenderedBitmap(document, None, obj, SCAN_SCALE)
        if not bitmap:
            return None
        try:
            if raw.FPDFBitmap_GetFormat(bitmap) != raw.FPDFBitmap_BGRA:
                return None
            width, height = raw.FPDFBitmap_GetWidth(bitmap), raw.FPDFBitmap_GetHeight(bitmap)
            stride = raw.FPDFBitmap_GetStride(bitmap)
            pixels = ctypes.string_at(raw.FPDFBitmap_GetBuffer(bitmap), stride * height)
            alpha = Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA", stride).getchannel("A")
            return _signature(alpha)
        finally:
            raw.FPDFBitmap_Destroy(bitmap)
    finally:
        raw.FPDFPageObj_Destroy(obj)


def _match(ink, pair, templates):
    distances = [ImageStat.Stat(ImageChops.difference(ink, sample)).mean[0] / 255 for sample in templates]
    first, second = sorted(range(2), key=distances.__getitem__)
    if distances[first] > 0.20 or distances[second] - distances[first] < 0.12:
        return None
    if pair == "“”":
        # Quotes are not Bidi_Mirrored. Recover the jointly witnessed font codepoint.
        return pair[first]
    sides = [_opening_side(sample) for sample in templates]
    if set(sides) != {True, False}:
        return None
    # For brackets, the caller uses pure RTL. The visual opener is a logical closer.
    return pair[int(sides[first])]


def _inline_actual_text(raw, obj):
    """Return single-glyph declarations only. Longer replacements are outside this check."""
    count = raw.FPDFPageObj_CountMarks(obj)
    if count < 0:
        return None
    declared = []
    for i in range(count):
        mark = raw.FPDFPageObj_GetMark(obj, i)
        value = raw.FPDFPageObjMark_GetParamValueType(mark, b"ActualText")
        if value == raw.FPDF_OBJECT_UNKNOWN:
            continue
        size = ctypes.c_ulong()
        if not raw.FPDFPageObjMark_GetParamStringValue(mark, b"ActualText", None, 0, ctypes.byref(size)) or size.value != 4:
            return None
        text = (raw.FPDF_WCHAR * 2)()
        if not raw.FPDFPageObjMark_GetParamStringValue(mark, b"ActualText", text, size.value, ctypes.byref(size)):
            return None
        declared.append(chr(text[0]))
    return declared


def _visible_glyph(raw, textpage, index):
    obj = raw.FPDFText_GetTextObject(textpage, index)
    if not obj or raw.FPDFTextObj_GetTextRenderMode(obj) != raw.FPDF_TEXTRENDERMODE_FILL:
        return None
    if raw.FPDFText_HasUnicodeMapError(textpage, index) != 0:
        return None
    matrix = raw.FS_MATRIX()
    if not raw.FPDFText_GetMatrix(textpage, index, ctypes.byref(matrix)):
        return None
    if not (matrix.a > 0 and matrix.d > 0 and matrix.b == matrix.c == 0):
        return None
    declared = _inline_actual_text(raw, obj)
    font = raw.FPDFTextObj_GetFont(obj)
    return (font, declared) if declared is not None and font and raw.FPDFFont_GetIsEmbedded(font) == 1 else None


def observe(page, candidates):
    """Tag candidates whose actual ink uniquely matches a pair from its own font."""
    from pypdfium2 import PdfiumError
    import pypdfium2.raw as raw

    frame = frame_of(page)
    if frame.page_rotate or ceil(frame.width * SCAN_SCALE) * ceil(frame.height * SCAN_SCALE) > MAX_SCAN_PIXELS:
        return
    if not hasattr(raw, "FPDFTextObj_GetRenderedBitmap"):
        return
    ink = None
    templates_by_pair = {}
    textpage = page.get_textpage()
    try:
        for atom in candidates:
            glyph = _visible_glyph(raw, textpage, atom.index)
            if glyph is None:
                continue
            font, declared = glyph
            pair = PDF_PUNCTUATION_PAIRS[atom.text]
            if any(char not in pair for char in declared):
                continue
            key = ctypes.cast(font, ctypes.c_void_p).value, pair
            if key not in templates_by_pair:
                templates_by_pair[key] = tuple(_template(raw, page.parent, font, ch) for ch in pair)
            templates = templates_by_pair[key]
            if any(template is None for template in templates):
                continue
            x0, y0, x1, y1 = frame.box(*textpage.get_charbox(atom.index))
            if not (0 <= x0 < x1 <= frame.width and 0 <= y0 < y1 <= frame.height):
                continue
            if ink is None:
                try:
                    rendered = page.render(scale=SCAN_SCALE, grayscale=True)
                except PdfiumError:
                    return
                try:
                    ink = ImageChops.invert(rendered.to_pil())
                finally:
                    rendered.close()
            bounds = (max(0, floor(x0 * SCAN_SCALE) - 1), max(0, floor(y0 * SCAN_SCALE) - 1),
                      min(ink.width, ceil(x1 * SCAN_SCALE) + 1), min(ink.height, ceil(y1 * SCAN_SCALE) + 1))
            sample = _signature(ink.crop(bounds))
            if sample is not None:
                expected = _match(sample, pair, templates)
                if all(char == expected for char in declared):
                    atom.pdf_punctuation = expected
    finally:
        textpage.close()
