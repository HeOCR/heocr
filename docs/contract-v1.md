# `heocr_document.v1`: the frozen contract

Every record `heocr` returns (`Document.to_dict()`, `doc.to_json()`, `heocr -f json`) is a
`heocr_document.v1` record. Since D5 its shape is frozen and checked two ways:

- **[`src/heocr/schemas/heocr_document.v1.schema.json`](../src/heocr/schemas/heocr_document.v1.schema.json)**
  is a JSON Schema (draft 2020-12). It ships with the package: `heocr.contract.json_schema()`.
  It describes every field and every closed list (block kinds, roles, sources, page classes,
  unread scopes).
- **`heocr.validate(record)`** checks the same, plus the rules a schema cannot state, because
  they need the whole record:
  - a page's `index` is its position;
  - a block's `id` is `b<position>`, and its `page` indexes `pages`;
  - a note reference's `offset` lies inside its block's text;
  - OCR text is `unverified`;
  - no file name or path appears anywhere in the provenance.

  Every record passes `validate` before it is returned.

The test suite checks every record it produces against both (`tests/__init__.py`, `tests/test_schema.py`).

## Changing v1

A consumer that reads v1 must never break on a later v1 record. So, within v1:

- **Allowed (additive):**
  - a new optional field in any object;
  - a new key in the open objects (`signals`, `coverage`, `checks`, `ocr`, `producer.components`, `options`);
  - a new value in an **open** list of strings: flags (record, page, block, cell), unread `reason`s, repair `rule`s, page `route`s, `document.format`s.

  Each such change updates the schema and validator when they name the field, and is noted in CHANGELOG.md under "Record".
- **Not allowed within v1. Any of these means `heocr_document.v2`:**
  - removing or renaming a field;
  - changing a field's type or meaning;
  - making an optional field required;
  - adding a value to a **closed** list: block `kind`, `role`, `source`, page `class`, unread `scope`, `direction`.

  A consumer may switch on these lists, so a new value can break it silently.
- **Moving to v2:**
  - the `schema` value becomes `heocr_document.v2`, with a new schema file next to v1's;
  - CHANGELOG.md says what changed and how to read v2 coming from v1;
  - for at least one minor release, heocr can still produce v1 on request, and the v1 schema and this document stay, marked deprecated with the release that will drop them.

The schema's objects are open (no `additionalProperties: false`) so that an older v1 schema keeps accepting a newer v1 record.
