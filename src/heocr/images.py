"""Images as input files (D4b): PNG, JPEG, TIFF (multi-page), GIF, BMP, WebP and JPEG 2000.

Each frame is a page of class T5, read by the OCR route with everything it does on a scanned
PDF page: ruled tables cell by cell (D3e), ink accounting, the number model for amounts.

- **Pages:** every frame of a TIFF is a page, in file order. An animated GIF is read as its
  first frame only, and says so (``image_frames_skipped``): its frames are animation, not pages.
- **Orientation:** EXIF orientation is applied before reading, as a viewer shows the image.
- **Resolution:** the file's own DPI when it gives one; otherwise 300 is assumed and the page is
  flagged ``dpi_assumed`` (Tesseract's results depend on it). Boxes are in points at that DPI.
- **Size:** a frame over ``MAX_RENDER_PIXELS`` is scaled down to it before OCR and flagged
  ``image_downscaled``; a hostile size cannot allocate unbounded memory, and Pillow's own
  decompression-bomb limit stays on.
- **Colour:** transparency is flattened onto white, and the frame is read in grey levels.
- **Pages:** at most ``max_pages`` frames are read (``contract.MAX_PAGES``, 2,000 by default);
  the rest are one ``unread`` entry (``page_limit``), and the document is flagged.
- **Nothing silent:** a frame without ink is an ``empty`` page; a file Pillow cannot decode, or
  a frame that fails to load, goes to ``unread`` with the reason.
"""

from __future__ import annotations

import io

from heocr.contract import Document, Page, Unread, page_limit, pages_left_unread, time_up
from heocr.ocr.regions import MAX_RENDER_PIXELS, RENDER_DPI, has_ink

MIN_DPI = 50  # a smaller declared resolution is a placeholder (JFIF's 1:1 aspect, 72 by default in some tools)
MAX_DPI = 2400
ASSUMED_DPI = RENDER_DPI


def _grey(frame):
    from PIL import Image, ImageOps

    frame = ImageOps.exif_transpose(frame)
    if frame.mode in ("RGBA", "LA", "PA") or (frame.mode == "P" and "transparency" in frame.info):
        rgba = frame.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        frame = Image.alpha_composite(white, rgba)
    return frame.convert("L")


def _dpi(frame) -> tuple[float, bool]:
    dpi = frame.info.get("dpi")
    if dpi:
        try:
            value = float(dpi[0] if isinstance(dpi, (tuple, list)) else dpi)
        except (TypeError, ValueError):
            value = 0.0
        if abs(value - round(value)) < 0.1:
            value = float(round(value))  # PNG stores pixels per metre: 150 dpi comes back as 149.99
        if MIN_DPI <= value <= MAX_DPI:
            return value, False
    return float(ASSUMED_DPI), True


def read_image(body: bytes, doc: Document) -> None:
    """Fill ``doc`` with the pages of an image file, each read by OCR."""
    from PIL import Image, UnidentifiedImageError

    from heocr.pdf.reader import PageText, _ocr_page

    try:
        import PIL

        doc.components["Pillow"] = PIL.__version__
        image = Image.open(io.BytesIO(body))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        doc.unread.append(Unread(scope="document", reason="image_unreadable", detail=f"{type(exc).__name__}: {exc}"[:300]))
        doc.flags.append("not_read")
        return
    total = getattr(image, "n_frames", 1)
    frames = total if (image.format or "").upper() in ("TIFF", "MPO") else 1
    if total > frames:
        doc.flags.append("image_frames_skipped")
    doc.page_count = frames
    pages = page_limit(doc, frames)
    for index in range(pages):
        if time_up(doc):
            pages_left_unread(doc, index, pages)
            break
        try:
            image.seek(index)
            frame = _grey(image.copy())
        except (OSError, ValueError, EOFError) as exc:
            doc.unread.append(Unread(scope="page", reason="image_unreadable", page=index, detail=f"frame {index}: {type(exc).__name__}"))
            continue
        dpi, assumed = _dpi(image)
        flags = ["dpi_assumed"] if assumed else []
        scale = dpi / 72  # pixels per point
        width, height = frame.size[0] / scale, frame.size[1] / scale
        if frame.size[0] * frame.size[1] > MAX_RENDER_PIXELS:
            factor = (MAX_RENDER_PIXELS / (frame.size[0] * frame.size[1])) ** 0.5
            frame = frame.resize((max(1, int(frame.size[0] * factor)), max(1, int(frame.size[1] * factor))))
            scale *= factor
            flags.append("image_downscaled")
        signals = {"image_format": (image.format or "").lower(), "frame": index, "dpi": round(dpi, 1), "pixels": list(frame.size)}
        if not has_ink(frame):
            doc.pages.append(Page(index, round(width, 2), round(height, 2), "empty", "none", signals, {"chars_total": 0, "chars_read": 0, "status": "nothing_to_read"}, flags))
            continue
        _ocr_page(doc, index, None, round(width, 2), round(height, 2), "T5", signals, PageText(), flags, rendered=(frame, scale))
