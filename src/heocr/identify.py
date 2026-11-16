"""File-type identification from content: magic bytes and container parts, never the file name.

Ported from tendex ``identify()`` (AdanimInstitute/adanim-govil-scraper, ``tendex_l4/extract.py``,
released for porting into heocr under Apache-2.0 by decision 0020, item 4), extended with ODF,
RTF, HTML, image and text detection, and with our own OLE2 directory reader in place of olefile.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from dataclasses import dataclass


@dataclass(frozen=True)
class FileKind:
    format: str
    evidence: str


OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")

_OOXML_PARTS = (
    ("word/document.xml", "docx"),
    ("xl/workbook.xml", "xlsx"),
    ("ppt/presentation.xml", "pptx"),
)
_ODF_MIMETYPES = {
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/epub+zip": "epub",
}
_IMAGES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"\x00\x00\x00\x0cjP  \r\n\x87\n", "jp2"),
)
_HTML = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*(?:<\?xml[^>]{0,200}\?>\s*)?(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html[\s>]|head[\s>]|body[\s>])", re.I | re.S)


def identify(body: bytes) -> FileKind:
    """Return the format of ``body`` and the evidence for it."""
    if not body:
        return FileKind("empty", "zero bytes")
    if b"%PDF-" in body[:1024]:
        return FileKind("pdf", "%PDF- header in the first 1024 bytes")
    if body.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return _zip_kind(body)
    if body.startswith(OLE_MAGIC):
        return _ole_kind(body)
    if body.lstrip(b"\xef\xbb\xbf").startswith(b"{\\rtf"):
        return FileKind("rtf", "{\\rtf header")
    for magic, kind in _IMAGES:
        if body.startswith(magic):
            return FileKind(kind, f"{kind} magic bytes")
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return FileKind("webp", "RIFF WEBP header")
    if _HTML.match(body[:4096]):
        return FileKind("html", "HTML start tag")
    if b"\x00" not in body[:65536]:
        try:
            body[:65536].decode("utf-8")
        except UnicodeDecodeError as exc:
            # A multi-byte character cut at the sample's end is still UTF-8.
            if exc.start < len(body[:65536]) - 3:
                return FileKind("unknown", "no known signature; not UTF-8 text")
        return FileKind("text", "UTF-8 text without NUL bytes")
    return FileKind("unknown", "no known signature")


def _zip_kind(body: bytes) -> FileKind:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = set(archive.namelist())
            mimetype = ""
            if "mimetype" in names:
                info = archive.getinfo("mimetype")
                if info.file_size <= 256:
                    mimetype = archive.read("mimetype").decode("ascii", "replace").strip()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return FileKind("unknown", f"ZIP signature but unreadable archive: {type(exc).__name__}")
    for part, kind in _OOXML_PARTS:
        if part in names:
            return FileKind(kind, f"ZIP container with {part}")
    if mimetype in _ODF_MIMETYPES:
        return FileKind(_ODF_MIMETYPES[mimetype], f"ZIP container with mimetype {mimetype}")
    return FileKind("zip", f"ZIP container with {len(names)} members and no office part")


def _ole_kind(body: bytes) -> FileKind:
    try:
        names = ole_stream_names(body)
    except ValueError as exc:
        return FileKind("ole_unknown", f"OLE2 signature but unreadable directory: {exc}")
    if "EncryptionInfo" in names and "EncryptedPackage" in names:
        return FileKind("ooxml_encrypted", "OLE2 container with EncryptedPackage")
    if "WordDocument" in names:
        return FileKind("doc", "OLE2 container with a WordDocument stream")
    if "Workbook" in names or "Book" in names:
        return FileKind("xls", "OLE2 container with a Workbook stream")
    if "PowerPoint Document" in names:
        return FileKind("ppt", "OLE2 container with a PowerPoint Document stream")
    if any(name.startswith("__substg1.0_") for name in names):
        return FileKind("msg", "OLE2 container with __substg1.0_ streams")
    return FileKind("ole_unknown", f"OLE2 container with {len(names)} directory entries")


# -- OLE2 (Compound File Binary) directory --------------------------------------
_FREE, _END, _FAT_SECTOR, _DIFAT_SECTOR = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
_MAX_SECTORS = 1 << 20


def ole_stream_names(body: bytes) -> set[str]:
    """Names of every entry in an OLE2 compound file's directory.

    Reads the header, the DIFAT and FAT, and follows the directory chain. Every
    sector index and chain length is bounded, so a damaged file raises ValueError.
    """
    if len(body) < 512 or not body.startswith(OLE_MAGIC):
        raise ValueError("too short for an OLE2 header")
    sector_shift = struct.unpack_from("<H", body, 30)[0]
    if sector_shift not in (9, 12):
        raise ValueError(f"sector shift {sector_shift}")
    size = 1 << sector_shift
    n_fat, first_dir = struct.unpack_from("<II", body, 44)[0], struct.unpack_from("<I", body, 48)[0]
    first_difat, n_difat = struct.unpack_from("<II", body, 68)
    if n_fat > _MAX_SECTORS or n_difat > _MAX_SECTORS:
        raise ValueError("implausible sector counts")
    n_sectors = (len(body) - size) // size

    def sector(index: int) -> bytes:
        if index >= n_sectors:
            raise ValueError(f"sector {index} beyond end of file")
        start = size + index * size
        return body[start : start + size]

    fat_sectors = [s for s in struct.unpack_from("<109I", body, 76) if s not in (_FREE, _END)]
    difat, seen = first_difat, set()
    per = size // 4 - 1
    while difat not in (_FREE, _END) and len(fat_sectors) < n_fat:
        if difat in seen:
            raise ValueError("DIFAT loop")
        seen.add(difat)
        values = struct.unpack(f"<{per + 1}I", sector(difat))
        fat_sectors += [s for s in values[:per] if s not in (_FREE, _END)]
        difat = values[per]
    fat: list[int] = []
    for index in fat_sectors[:n_fat]:
        fat += struct.unpack(f"<{size // 4}I", sector(index))

    names: set[str] = set()
    current, visited = first_dir, set()
    while current not in (_FREE, _END):
        if current in visited or len(visited) > n_sectors:
            raise ValueError("directory chain loop")
        visited.add(current)
        data = sector(current)
        for offset in range(0, size, 128):
            entry = data[offset : offset + 128]
            name_len = struct.unpack_from("<H", entry, 64)[0]
            kind = entry[66]
            if kind == 0 or not 2 <= name_len <= 64:
                continue
            names.add(entry[: name_len - 2].decode("utf-16-le", "replace"))
        if current >= len(fat):
            raise ValueError("directory sector outside the FAT")
        current = fat[current]
    return names
