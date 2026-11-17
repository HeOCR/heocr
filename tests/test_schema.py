"""D5b: heocr_document.v1 frozen as a JSON Schema (src/heocr/schemas/heocr_document.v1.schema.json).

Every record the suite produces is checked against it (tests/__init__.py). These tests check the
schema itself: valid JSON Schema, in step with the contract's own lists, and rejecting what
``heocr.validate`` rejects for the rules both can express."""

import copy
import unittest

import heocr
from heocr import contract
from tests import RECORDS_SCHEMA_CHECKED
from tests.synth.pdf import PdfPage, lines_block, make_pdf

try:
    import jsonschema
except ImportError:
    jsonschema = None


@unittest.skipIf(jsonschema is None, 'needs jsonschema: pip install ".[test]"')
class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = contract.json_schema()
        self.validator = jsonschema.Draft202012Validator(self.schema)
        self.record = heocr.extract(make_pdf([PdfPage(lines=lines_block(["שלום עולם"]))]), ocr=False).to_dict()

    def test_the_schema_is_valid_and_shipped(self) -> None:
        jsonschema.Draft202012Validator.check_schema(self.schema)
        self.assertEqual(self.schema["title"], contract.SCHEMA_ID)
        self.assertTrue(RECORDS_SCHEMA_CHECKED)

    def test_its_lists_are_the_contracts(self) -> None:
        d = self.schema["$defs"]
        self.assertEqual(tuple(d["block"]["properties"]["kind"]["enum"]), contract.BLOCK_KINDS)
        self.assertEqual(tuple(d["block"]["properties"]["role"]["enum"]), contract.ROLES)
        self.assertEqual(tuple(d["block"]["properties"]["source"]["enum"]), contract.SOURCES)
        self.assertEqual(tuple(d["page"]["properties"]["class"]["enum"]), contract.PAGE_CLASSES)
        self.assertEqual(tuple(d["unread"]["properties"]["scope"]["enum"]), contract.UNREAD_SCOPES)
        self.assertEqual(d["cell"]["properties"]["colspan"]["maximum"], contract.MAX_SPAN)

    def test_a_record_validates(self) -> None:
        self.assertEqual(list(self.validator.iter_errors(self.record)), [])

    def test_it_rejects_what_validate_rejects(self) -> None:
        def broken(change):
            payload = copy.deepcopy(self.record)
            change(payload)
            return payload

        cases = {
            "schema id": lambda p: p.update(schema="heocr_document.v2"),
            "producer": lambda p: p["producer"].update(name="other"),
            "sha256": lambda p: p["document"].update(sha256="xyz"),
            "a file name": lambda p: p["document"].update(filename="tender.pdf"),
            "page class": lambda p: p["pages"][0].update({"class": "T9"}),
            "block role": lambda p: p["blocks"][0].update(role="sidebar"),
            "block source": lambda p: p["blocks"][0].update(source="magic"),
            "table without cells": lambda p: p["blocks"][0].update(kind="table"),
            "ocr without unverified": lambda p: p["blocks"][0].update(source="ocr", ocr={"status": "verified"}),
            "unread scope": lambda p: p["unread"].append({"scope": "line", "reason": "x"}),
            "bad box": lambda p: p["blocks"][0].update(bbox=[1, 2, 3]),
        }
        for name, change in cases.items():
            with self.subTest(name):
                payload = broken(change)
                self.assertNotEqual(heocr.validate(payload), [])
                self.assertNotEqual(list(self.validator.iter_errors(payload)), [])

    def test_objects_are_open_for_additive_fields(self) -> None:
        # Within v1 a field may be added (docs/contract-v1.md): an older v1 schema still accepts it.
        payload = copy.deepcopy(self.record)
        payload["blocks"][0]["new_field_in_a_later_v1"] = 1
        payload["pages"][0]["coverage"]["new_count"] = 3
        self.assertEqual(list(self.validator.iter_errors(payload)), [])


if __name__ == "__main__":
    unittest.main()
