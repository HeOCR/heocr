import copy
import json
import unittest

import heocr
from heocr.contract import SCHEMA_ID, Block, Cell, ContractError, Document, Line, Page, Unread, validate


def _doc() -> Document:
    doc = Document(sha256="a" * 64, size=10, format="pdf", format_evidence="%PDF- header", page_count=1)
    doc.pages.append(Page(index=0, width=595, height=842, page_class="T1", route="text_layer"))
    doc.blocks.append(
        Block(
            kind="text",
            text="שלום עולם",
            page=0,
            bbox=[10, 10, 100, 20],
            source="text_layer",
            tool="heocr.pdf",
            lines=[Line("שלום עולם", [10, 10, 100, 20])],
        )
    )
    doc.blocks.append(
        Block(kind="table", cells=[[Cell("א"), Cell("ב\nג", colspan=2)]], source="office_xml", direction="rtl")
    )
    return doc


class ContractTest(unittest.TestCase):
    def test_round_trip_is_valid(self) -> None:
        payload = json.loads(_doc().to_json())
        self.assertEqual(payload["schema"], SCHEMA_ID)
        self.assertEqual(validate(payload), [])
        self.assertEqual(payload["blocks"][0]["id"], "b0")
        self.assertEqual(payload["blocks"][1]["cells"][0][1], {"text": "ב\nג", "colspan": 2})
        self.assertEqual(payload["producer"]["version"], heocr.__version__)

    def test_rejects_file_names_and_paths_in_provenance(self) -> None:
        payload = _doc().to_dict()
        bad = copy.deepcopy(payload)
        bad["document"]["filename"] = "tender.pdf"
        self.assertTrue(any("filename" in e for e in validate(bad)))
        bad = copy.deepcopy(payload)
        bad["blocks"][0]["tool"] = "/Users/someone/heocr"
        self.assertTrue(any("filesystem path" in e for e in validate(bad)))
        bad = copy.deepcopy(payload)
        bad["unread"] = [{"scope": "document", "reason": "x", "detail": "C:\\docs\\a.pdf"}]
        self.assertTrue(any("filesystem path" in e for e in validate(bad)))

    def test_content_may_mention_paths(self) -> None:
        doc = _doc()
        doc.blocks[0].text = "ראו /home/user/readme"
        self.assertEqual(validate(doc.to_dict()), [])

    def test_invalid_enums_and_indexes(self) -> None:
        payload = _doc().to_dict()
        bad = copy.deepcopy(payload)
        bad["pages"][0]["class"] = "T9"
        bad["blocks"][0]["kind"] = "paragraph"
        bad["blocks"][0]["page"] = 3
        bad["blocks"][1]["id"] = "b7"
        errors = validate(bad)
        for needle in ("pages[0].class", "blocks[0].kind", "blocks[0].page", "blocks[1].id"):
            self.assertTrue(any(needle in e for e in errors), needle)

    def test_to_dict_refuses_an_invalid_record(self) -> None:
        doc = _doc()
        doc.blocks[0].role = "sidebar"
        with self.assertRaises(ContractError):
            doc.to_dict()

    def test_unread_is_recorded(self) -> None:
        doc = _doc()
        doc.unread.append(Unread(scope="page", reason="class_T5_no_route", page=0))
        payload = doc.to_dict()
        self.assertEqual(payload["unread"], [{"scope": "page", "reason": "class_T5_no_route", "page": 0}])

    def test_not_an_object(self) -> None:
        self.assertEqual(validate([]), ["record is not an object"])


class ExtractTest(unittest.TestCase):
    def test_unknown_input_is_reported_not_raised(self) -> None:
        doc = heocr.extract(b"just some bytes")
        payload = doc.to_dict()
        self.assertEqual(payload["blocks"], [])
        self.assertEqual(payload["unread"][0]["reason"], "format_not_supported")
        self.assertIn("not_read", payload["flags"])
        self.assertEqual(doc.text, "")


class CellReadingsTest(unittest.TestCase):
    def test_readings_must_name_a_model_and_a_text(self) -> None:
        doc = _doc()
        cell = Cell("100", readings=[{"model": "heb", "text": "00"}, {"model": "eng", "text": "100", "confidence": 95.0}])
        doc.blocks.append(Block(kind="table", cells=[[cell]], page=0, source="ocr", tool="t", ocr={"status": "unverified"}))
        self.assertEqual(heocr.validate(doc.to_dict()), [])
        data = doc.to_dict()
        data["blocks"][-1]["cells"][0][0]["readings"] = [{"text": "00"}]
        self.assertTrue(any("readings" in e for e in heocr.validate(data)))


if __name__ == "__main__":
    unittest.main()
