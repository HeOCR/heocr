import io
import unittest
import zipfile

import heocr
from heocr.identify import identify, ole_stream_names
from tests.synth.cfb import make_cfb


def _zip(members: dict[str, bytes], mimetype: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if mimetype is not None:
            archive.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class IdentifyTest(unittest.TestCase):
    def assertKind(self, body: bytes, expected: str) -> None:
        self.assertEqual(identify(body).format, expected, identify(body))

    def test_pdf_header_may_follow_junk(self) -> None:
        self.assertKind(b"%PDF-1.7\n", "pdf")
        self.assertKind(b"\x00" * 300 + b"%PDF-1.4", "pdf")
        self.assertKind(b"\x00" * 1100 + b"%PDF-1.4", "unknown")

    def test_ooxml_by_part_not_extension(self) -> None:
        self.assertKind(_zip({"[Content_Types].xml": b"", "word/document.xml": b"<w/>"}), "docx")
        self.assertKind(_zip({"xl/workbook.xml": b""}), "xlsx")
        self.assertKind(_zip({"ppt/presentation.xml": b""}), "pptx")
        self.assertKind(_zip({"a.txt": b"x"}), "zip")

    def test_odf_and_epub_by_mimetype(self) -> None:
        self.assertKind(_zip({"content.xml": b""}, "application/vnd.oasis.opendocument.text"), "odt")
        self.assertKind(_zip({}, "application/epub+zip"), "epub")

    def test_broken_zip(self) -> None:
        kind = identify(b"PK\x03\x04" + b"\x00" * 40)
        self.assertEqual(kind.format, "unknown")
        self.assertIn("unreadable archive", kind.evidence)

    def test_ole_containers(self) -> None:
        self.assertKind(make_cfb(["WordDocument", "1Table", "\x05SummaryInformation"]), "doc")
        self.assertKind(make_cfb(["Workbook"]), "xls")
        self.assertKind(make_cfb(["PowerPoint Document"]), "ppt")
        self.assertKind(make_cfb(["__substg1.0_0037001F"]), "msg")
        self.assertKind(make_cfb(["EncryptionInfo", "EncryptedPackage"]), "ooxml_encrypted")
        self.assertKind(make_cfb(["Something"]), "ole_unknown")

    def test_ole_directory_spanning_sectors(self) -> None:
        names = [f"Stream{i}" for i in range(9)] + ["WordDocument"]
        self.assertEqual(ole_stream_names(make_cfb(names)), set(names) | {"Root Entry"})

    def test_damaged_ole_is_reported(self) -> None:
        body = bytearray(make_cfb(["WordDocument"]))
        body[48:52] = (999).to_bytes(4, "little")  # directory sector beyond the file
        kind = identify(bytes(body))
        self.assertEqual(kind.format, "ole_unknown")
        self.assertIn("unreadable directory", kind.evidence)

    def test_other_signatures(self) -> None:
        self.assertKind(b"{\\rtf1\\ansi", "rtf")
        self.assertKind(b"\x89PNG\r\n\x1a\n....", "png")
        self.assertKind(b"\xff\xd8\xff\xe0", "jpeg")
        self.assertKind(b"II*\x00", "tiff")
        self.assertKind(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp")
        self.assertKind(b"\xef\xbb\xbf  <!-- x --><!DOCTYPE html><html>", "html")
        self.assertKind("<html lang=he><body>שלום".encode(), "html")
        self.assertKind("שלום עולם\n".encode(), "text")
        self.assertKind(b"", "empty")
        self.assertKind(b"\x00\x01\x02\x03", "unknown")
        self.assertKind("שלום".encode("cp1255") + b"\x00", "unknown")

    def test_extract_records_format_and_evidence(self) -> None:
        payload = heocr.extract(make_cfb(["WordDocument"])).to_dict()
        self.assertEqual(payload["document"]["format"], "doc")
        self.assertIn("WordDocument", payload["document"]["format_evidence"])


if __name__ == "__main__":
    unittest.main()
