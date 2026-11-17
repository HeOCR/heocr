"""Test package. With jsonschema installed (``pip install ".[test]"``, as CI does), every record the
suite produces is also checked against heocr_document.v1's JSON Schema: Document.to_dict is wrapped
so a record that ``heocr.validate`` accepts but the schema does not fails the test that made it."""

try:
    import jsonschema
except ImportError:  # pragma: no cover - the schema tests skip without it
    jsonschema = None

if jsonschema is not None:
    from heocr import contract

    _VALIDATOR = jsonschema.Draft202012Validator(contract.json_schema())
    _to_dict = contract.Document.to_dict

    def _checked_to_dict(self):
        payload = _to_dict(self)
        errors = sorted(_VALIDATOR.iter_errors(payload), key=lambda e: list(e.absolute_path))
        if errors:
            raise AssertionError("record does not match heocr_document.v1.schema.json: "
                                 + "; ".join(f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errors[:5]))
        return payload

    contract.Document.to_dict = _checked_to_dict
    RECORDS_SCHEMA_CHECKED = True
else:  # pragma: no cover
    RECORDS_SCHEMA_CHECKED = False
