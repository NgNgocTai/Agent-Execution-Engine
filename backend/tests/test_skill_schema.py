import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from services.skill_pipeline import manifest_hash, normalize_output
from services.skill_schema import (
    MAX_DEPTH,
    SchemaError,
    ValueMismatch,
    validate_schema,
    validate_value,
)
from services.skill_validation import InputField, Manifest, ScriptSpec, inspect_archive
from test_skill_safety import archive

# Hash of the pre-schema fixture manifest; approved skills must keep verifying.
LEGACY_MANIFEST_HASH = "89ed4cf8919c47d95abfd35747ab42ba4acceba3be3d6d1b955386899f594455"
RECORD = {
    "type": "object",
    "properties": {
        "cell": {"type": "string", "maxLength": 20},
        "drop_rate": {"type": "number", "minimum": 0, "maximum": 100},
        "state": {"type": "string", "enum": ["up", "down"]},
    },
    "required": ["cell", "drop_rate"],
}
RECORDS = {"type": "array", "items": RECORD, "maxItems": 500}
SUMMARY = {
    "type": "object",
    "properties": {
        "worst": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "count": {"type": "integer", "minimum": 0},
    },
    "required": ["worst", "count"],
}


def spec(**overrides):
    base = {
        "entrypoint": "scripts/triage.py",
        "input_fields": [
            {"name": "records", "kind": "array", "shape": RECORDS},
            {"name": "limit", "kind": "integer"},
        ],
        "sample_input": {"records": [{"cell": "c1", "drop_rate": 1.5}], "limit": 3},
        "output_contract": "json",
        "output_schema": SUMMARY,
    }
    return ScriptSpec.model_validate({**base, **overrides})


def nested(depth):
    node = {"type": "string"}
    for _ in range(depth - 1):
        node = {"type": "array", "items": node}
    return node


class StructuredInputTest(unittest.TestCase):
    def test_records_validate_and_model_sees_closed_schema(self):
        model = spec().input_model()
        parsed = model.model_validate(spec().sample_input)
        self.assertEqual(parsed.records[0]["cell"], "c1")
        schema = model.model_json_schema()
        item = schema["properties"]["records"]["items"]
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(item["required"], ["cell", "drop_rate"])
        self.assertIs(schema["additionalProperties"], False)

    def test_invalid_records_are_rejected_without_echoing_values(self):
        model = spec().input_model()
        secret = "hunter2-token"
        cases = {
            "extra field": [{"cell": "c", "drop_rate": 1, "note": secret}],
            "missing field": [{"cell": "c"}],
            "wrong type": [{"cell": "c", "drop_rate": secret}],
            "bool as number": [{"cell": "c", "drop_rate": True}],
            "range": [{"cell": "c", "drop_rate": 101}],
            "nan": [{"cell": "c", "drop_rate": float("nan")}],
            "infinity": [{"cell": "c", "drop_rate": float("inf")}],
            "enum": [{"cell": "c", "drop_rate": 1, "state": secret}],
            "long string": [{"cell": "x" * 21, "drop_rate": 1}],
            "not a list": {"cell": "c"},
            "too many items": [{"cell": "c", "drop_rate": 1}] * 501,
        }
        for name, records in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError) as raised:
                model.model_validate({"records": records, "limit": 1})
            self.assertNotIn(secret, str(raised.exception.errors(include_input=False)))

    def test_optional_array_defaults_to_empty_and_optional_object_is_safe(self):
        optional = spec(
            input_fields=[
                {"name": "records", "kind": "array", "shape": RECORDS, "required": False}
            ],
            sample_input={},
        )
        self.assertEqual(optional.input_model().model_validate({}).records, [])
        with self.assertRaises(ValidationError):
            InputField(name="record", kind="object", shape=RECORD, required=False)

    def test_aggregate_input_size_is_bounded(self):
        long_text = {"type": "array", "items": {"type": "string"}, "maxItems": 100}
        model = spec(
            input_fields=[{"name": "lines", "kind": "array", "shape": long_text}],
            sample_input={"lines": []},
            output_schema=None,
        ).input_model()
        with self.assertRaises(ValidationError):
            model.model_validate({"lines": ["x" * 10000] * 100})
        model.model_validate({"lines": ["x" * 10000] * 20})

    def test_kind_and_shape_must_agree(self):
        for payload in (
            {"name": "a", "kind": "array"},
            {"name": "a", "kind": "array", "shape": SUMMARY},
            {"name": "a", "kind": "string", "shape": RECORDS},
        ):
            with self.subTest(payload), self.assertRaises(ValidationError):
                InputField.model_validate(payload)


class SchemaBoundsTest(unittest.TestCase):
    def test_unsupported_or_unbounded_declarations_fail_closed(self):
        too_many_properties = {
            "type": "object",
            "properties": {f"p{i}": {"type": "string"} for i in range(51)},
        }
        wide = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    f"p{i}": {"type": "array", "items": {"type": "string"}}
                    for i in range(50)
                },
            },
        }
        cases = {
            "depth": nested(MAX_DEPTH + 1),
            "properties": too_many_properties,
            "nodes": {"type": "array", "items": {"type": "array", "items": wide}},
            "pattern": {"type": "string", "pattern": "(a+)+$"},
            "ref": {"$ref": "#/definitions/x"},
            "oneOf": {"oneOf": [{"type": "string"}]},
            "open object": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": True,
            },
            "empty object": {"type": "object", "properties": {}},
            "required typo": {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["b"],
            },
            "bad property name": {
                "type": "object",
                "properties": {"a b": {"type": "string"}},
            },
            "oversized array": {"type": "array", "items": {"type": "string"}, "maxItems": 10**6},
            "inverted range": {"type": "number", "minimum": 2, "maximum": 1},
            "nan bound": {"type": "number", "minimum": float("nan")},
            "enum mismatch": {"type": "integer", "enum": ["a"]},
            "missing items": {"type": "array"},
        }
        for name, schema in cases.items():
            with self.subTest(name), self.assertRaises(SchemaError):
                validate_schema(schema)
        validate_schema(nested(MAX_DEPTH))

    def test_value_limits(self):
        with self.assertRaises(ValueMismatch):
            validate_value(
                {"type": "array", "items": {"type": "integer"}, "maxItems": 1000},
                list(range(1001)),
            )
        with self.assertRaises(ValueMismatch):
            validate_value({"type": "integer"}, 1.0)
        with self.assertRaises(ValueMismatch):
            validate_value({"type": "number"}, float("nan"))


class OutputSchemaTest(unittest.TestCase):
    def output(self, data, contract="json", schema=SUMMARY):
        return normalize_output(json.dumps(data).encode(), contract, schema)

    def test_valid_shape_passes_for_json_and_skill_result(self):
        good = {"worst": ["c1"], "count": 1}
        self.assertEqual(self.output(good).data, good)
        self.assertEqual(
            self.output({"status": "success", "data": good}, "skill_result").data, good
        )

    def test_valid_json_with_wrong_shape_fails(self):
        for name, data in {
            "wrong type": {"worst": [1], "count": 1},
            "missing": {"worst": []},
            "extra": {"worst": [], "count": 0, "debug": "x"},
            "not object": [1],
            "range": {"worst": [], "count": -1},
        }.items():
            with self.subTest(name), self.assertRaises(ValueError) as raised:
                self.output(data)
            self.assertIn("reviewed schema", str(raised.exception))

    def test_skill_result_with_wrong_data_shape_fails(self):
        with self.assertRaises(ValueError):
            self.output(
                {"status": "success", "data": {"worst": "c1", "count": 1}}, "skill_result"
            )

    def test_without_schema_behavior_is_unchanged(self):
        self.assertEqual(self.output({"anything": 1}, schema=None).data, {"anything": 1})

    def test_text_contract_only_accepts_string_schema(self):
        with self.assertRaises(ValidationError):
            spec(output_contract="text", output_schema=SUMMARY)
        text = normalize_output(b"ok", "text", {"type": "string", "maxLength": 10})
        self.assertEqual(text.data, "ok")
        with self.assertRaises(ValueError):
            normalize_output(b"x" * 11, "text", {"type": "string", "maxLength": 10})

    def test_invalid_output_schema_is_rejected_at_review_time(self):
        with self.assertRaises(ValidationError):
            spec(output_schema={"type": "object", "properties": {}})


class CompatibilityTest(unittest.TestCase):
    def test_manifests_without_schemas_keep_their_reviewed_hash(self):
        manifest, _, _ = inspect_archive(archive())
        self.assertEqual(manifest_hash(manifest), LEGACY_MANIFEST_HASH)

    def test_schema_changes_change_the_reviewed_hash(self):
        manifest, _, _ = inspect_archive(archive())
        with_output = manifest.model_copy(update={"output_schema": SUMMARY})
        self.assertNotEqual(manifest_hash(with_output), LEGACY_MANIFEST_HASH)
        changed = manifest.model_copy(
            update={"output_schema": {**SUMMARY, "required": ["count"]}}
        )
        self.assertNotEqual(manifest_hash(with_output), manifest_hash(changed))

    def test_structured_input_shape_is_part_of_the_hash(self):
        def manifest(shape):
            return Manifest.model_validate(
                {
                    "name": "custom_triage",
                    "version": "1.0.0",
                    "description": "Triage supplied KPI records",
                    "scripts": [
                        spec(
                            input_fields=[
                                {"name": "records", "kind": "array", "shape": shape},
                                {"name": "limit", "kind": "integer"},
                            ]
                        ).model_dump()
                    ],
                }
            )

        self.assertNotEqual(
            manifest_hash(manifest(RECORDS)),
            manifest_hash(manifest({**RECORDS, "maxItems": 5})),
        )


if __name__ == "__main__":
    unittest.main()
