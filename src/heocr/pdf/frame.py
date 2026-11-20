"""The visible part of a PDF page, as a viewer shows it, and coordinates within it.

pdfium reports character, image and path positions in the PDF's own user space, whatever the
page box or its rotation. heocr reads a page in **one frame**, and every position it records is
in that frame: the top-left corner of the visible box is (0, 0), and y grows downwards.

- **The visible box** is the CropBox (the MediaBox when there is none), clipped to the
  MediaBox. Its origin need not be (0, 0): a box such as ``[-612 0 0 792]`` is a normal page
  whose content sits at negative x.
- **The displayed frame** applies ``/Rotate`` (clockwise), as a viewer does. A page whose text is
  upright on screen is stored with its characters turned in user space; in the displayed frame
  they are upright, and are read like any other. Renders for OCR are as displayed too, so region
  crops, masks of layer characters, table cells and number checks are upright for OCR.
- **The user frame**, per page, by the layer's own evidence: when a rotated page's characters
  are mostly upright in **user space** (at least ``SIDEWAYS_SHARE`` of at least
  ``MIN_ORIENTATION_CHARS`` characters), the page is sideways on screen; reading it as displayed
  would lose text read today. It is read in the user frame, rendered in that frame as well, and
  flagged ``text_sideways_on_screen``. A mixed page (neither frame holds that share) is read as
  displayed; its other characters are counted as ``rotated_text_not_read``.
- **/UserUnit** scales the whole user space uniformly: it changes no decision here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ROTATED_SIN = 0.1  # a character is upright when the sine of its angle is at most this (and its cosine positive)
SIDEWAYS_SHARE = 0.8
MIN_ORIENTATION_CHARS = 20
SAMPLE_CHARS = 4000


@dataclass(frozen=True)
class Frame:
    x0: float  # the visible box's left edge, in user space
    top: float  # its top edge, in user space
    box_width: float  # the visible box, unrotated
    box_height: float
    rotation: int = 0  # clockwise degrees applied to user space: the page's /Rotate, or 0 in the user frame
    page_rotate: int = 0  # the page's /Rotate, whatever the frame

    @property
    def sideways(self) -> bool:
        """The page is read in the user frame although it is rotated on screen."""
        return self.page_rotate != 0 and self.rotation == 0

    @property
    def width(self) -> float:
        return self.box_height if self.rotation in (90, 270) else self.box_width

    @property
    def height(self) -> float:
        return self.box_width if self.rotation in (90, 270) else self.box_height

    def point(self, x: float, y: float) -> tuple[float, float]:
        """A user-space point in this frame: origin at the top left, y downwards."""
        u, v = x - self.x0, self.top - y  # the unrotated box, top-left origin
        if self.rotation == 90:
            return self.box_height - v, u
        if self.rotation == 180:
            return self.box_width - u, self.box_height - v
        if self.rotation == 270:
            return v, self.box_width - u
        return u, v

    def box(self, left: float, bottom: float, right: float, top: float) -> tuple[float, float, float, float]:
        """A user-space box (left, bottom, right, top) as (x0, y0, x1, y1) in this frame."""
        (ax, ay), (bx, by) = self.point(left, bottom), self.point(right, top)
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))

    def angle(self, user_angle: float) -> float:
        """A character's angle (pdfium's, in radians) in this frame."""
        return user_angle + math.radians(self.rotation)

    def outside(self, box: tuple[float, float, float, float]) -> bool:
        """Whether a box (in this frame) has its centre outside the visible box."""
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return not (0.0 <= cx <= self.width and 0.0 <= cy <= self.height)


def upright(angle: float) -> bool:
    return abs(math.sin(angle)) <= ROTATED_SIN and math.cos(angle) > 0


def _layer_mostly_upright_in_user_space(page) -> bool:
    """Whether a page's text-layer characters are mostly upright in user space (sideways on screen)."""
    import pypdfium2.raw as raw

    textpage = page.get_textpage()
    try:
        count = min(raw.FPDFText_CountChars(textpage), SAMPLE_CHARS)
        total = user = 0
        for index in range(count):
            code = raw.FPDFText_GetUnicode(textpage, index)
            if code <= 32 or not raw.FPDFText_GetTextObject(textpage, index):
                continue
            angle = raw.FPDFText_GetCharAngle(textpage, index)
            if angle < 0:  # pdfium's error value
                continue
            total += 1
            user += upright(angle)
    finally:
        textpage.close()
    return total >= MIN_ORIENTATION_CHARS and user >= SIDEWAYS_SHARE * total


def frame_of(page) -> Frame:
    """The frame a page is read in: its visible box, displayed (``/Rotate`` applied) unless its
    layer is sideways on screen (see the module's documentation)."""
    media = page.get_mediabox()
    crop = page.get_cropbox()  # pypdfium2 falls back to the MediaBox
    x0, y0 = max(media[0], crop[0]), max(media[1], crop[1])
    x1, y1 = min(media[2], crop[2]), min(media[3], crop[3])
    if x1 <= x0 or y1 <= y0:  # a crop box outside the media box: viewers show the media box
        x0, y0, x1, y1 = media
    rotate = page.get_rotation() % 360
    rotation = rotate
    if rotate and _layer_mostly_upright_in_user_space(page):
        rotation = 0
    return Frame(x0, y1, x1 - x0, y1 - y0, rotation, rotate)
