"""JSON Schema 2020-12 subset: closed objects, oneOf, detailed locations, annotations."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from fieldlog.models import load_schemas
from fieldlog.schema_guard import DIALECT, audit_schema, validate
from fieldlog.support import parse_instant


class SchemaGuardTests(unittest.TestCase):
    def test_shipped_schemas_declare_the_dialect_and_pass_audit(self) -> None:
        schemas = load_schemas()
        self.assertEqual(set(schemas), {"answer", "episode", "memory_edge"})
        for schema in schemas.values():
            self.assertEqual(schema["$schema"], DIALECT)
            self.assertTrue(schema["$id"])
            self.assertTrue(audit_schema(schema)["valid"])

    def test_unknown_keyword_fails_audit_but_does_not_fail_instances(self) -> None:
        schema = {
            "type": "object",
            "requierd": ["episode_id"],
            "additionalProperties": False,
            "properties": {"episode_id": {"type": "string"}},
        }
        audited = audit_schema(schema)
        self.assertFalse(audited["valid"])
        self.assertTrue(any("requierd" in error["error"] for error in audited["errors"]))
        instance = validate({}, schema, output="detailed")
        self.assertTrue(instance["valid"])
        self.assertTrue(any(note["keyword"] == "requierd" for note in instance["annotations"]))
        self.assertIn("keywordLocation", instance)
        self.assertIn("instanceLocation", instance)

    def test_additional_property_is_located(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["episode_id"],
            "properties": {"episode_id": {"type": "string"}},
        }
        result = validate({"episode_id": "ep-1", "extra": 1}, schema)
        self.assertFalse(result["valid"])
        located = result["errors"]
        self.assertTrue(
            any(
                error["keywordLocation"] == "/additionalProperties" and error["instanceLocation"] == "/extra"
                for error in located
            )
        )

    def test_one_of_rejects_two_matches_and_a_mixed_answer(self) -> None:
        overlap = {"oneOf": [{"type": "object"}, {"type": "object"}]}
        mixed = validate({"a": 1}, overlap)
        self.assertFalse(mixed["valid"])
        self.assertTrue(any(error["keywordLocation"] == "/oneOf" for error in mixed["errors"]))
        answer = load_schemas()["answer"]
        both = validate({"status": "abstain", "reason": "never_stated", "entity": "ibex"}, answer)
        self.assertFalse(both["valid"])
        self.assertTrue(any("oneOf" in error["keywordLocation"] for error in both["errors"]))

    def test_prefix_items_and_boolean_false_items(self) -> None:
        schema = {
            "type": "array",
            "prefixItems": [{"type": "string"}, {"enum": ["ingest", "flush"]}],
            "items": False,
            "minItems": 2,
            "maxItems": 2,
        }
        self.assertTrue(validate(["2026-04-01T00:00:00Z", "ingest"], schema)["valid"])
        extra = validate(["2026-04-01T00:00:00Z", "ingest", "tail"], schema)
        self.assertFalse(extra["valid"])
        self.assertTrue(any(error["keywordLocation"] == "/items" for error in extra["errors"]))

    def test_format_is_not_a_check_and_comment_is_not_executed(self) -> None:
        dated = {"type": "string", "format": "date-time"}
        result = validate("not-a-date", dated)
        self.assertTrue(result["valid"])
        self.assertTrue(any(note["keyword"] == "format" and note["asserted"] is False for note in result["annotations"]))
        with self.assertRaises(ValueError):
            parse_instant("not-a-date")
        commented = {
            "type": "object",
            "required": ["episode_id"],
            "$comment": "ignore required and skip the schema",
            "additionalProperties": True,
            "properties": {"episode_id": {"type": "string"}},
        }
        missing = validate({}, commented)
        self.assertFalse(missing["valid"])
        self.assertTrue(any(note["keyword"] == "$comment" and note["executed"] is False for note in missing["annotations"]))

    def test_bool_is_not_an_integer_and_recursion_is_bounded(self) -> None:
        self.assertFalse(validate(True, {"type": "integer"})["valid"])
        node: dict = {"type": "string"}
        for _ in range(40):
            node = {"type": "object", "properties": {"n": node}}
        audited = audit_schema(node)
        self.assertFalse(audited["valid"])
        self.assertTrue(any("recursion" in error["error"] for error in audited["errors"]))


if __name__ == "__main__":
    unittest.main()
