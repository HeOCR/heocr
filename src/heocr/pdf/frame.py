"""The visible part of a PDF page, as a viewer shows it, and coordinates within it.

pdfium reports character, image and path positions in the PDF's own user space, whatever the
page box. A viewer shows the **CropBox** (the MediaBox when there is none), clipped to the
MediaBox, and its origin need not be (0, 0): a box such as ``[-612 0 0 792]`` is a normal page
whose content sits at negative x. Every position heocr records is relative to the top-left
corner of that visible box, so such a page reads like any other, and only what lies outside the
box is outside the page.

- **/Rotate** turns the shown page, box and content together: it does not change what is inside.
  Positions stay in the unrotated box, as before; the page is flagged ``page_rotated``.
- **/UserUnit** scales the whole user space uniformly: it changes no inside/outside decision.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    x0: float  # the visible box's left edge, in user space
    top: float  # its top edge, in user space
    width: float
    height: float

    def box(self, left: float, bottom: float, right: float, top: float) -> tuple[float, float, float, float]:
        """A user-space box as (x0, y0, x1, y1) in the visible box, origin at its top left."""
        return (left - self.x0, self.top - top, right - self.x0, self.top - bottom)

    def y(self, user_y: float) -> float:
        return self.top - user_y

    def x(self, user_x: float) -> float:
        return user_x - self.x0

    def outside(self, box: tuple[float, float, float, float]) -> bool:
        """Whether a box (in visible-box coordinates) has its centre outside the visible box."""
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        return not (0.0 <= cx <= self.width and 0.0 <= cy <= self.height)


def frame_of(page) -> Frame:
    """The visible box of a pypdfium2 page: its CropBox (else MediaBox) clipped to the MediaBox."""
    media = page.get_mediabox()
    crop = page.get_cropbox()  # pypdfium2 falls back to the MediaBox
    x0, y0 = max(media[0], crop[0]), max(media[1], crop[1])
    x1, y1 = min(media[2], crop[2]), min(media[3], crop[3])
    if x1 <= x0 or y1 <= y0:  # a crop box outside the media box: viewers show the media box
        x0, y0, x1, y1 = media
    return Frame(x0, y1, x1 - x0, y1 - y0)
