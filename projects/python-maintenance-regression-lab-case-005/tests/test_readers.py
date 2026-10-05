"""Strict, tolerant, and additionalProperties-false readers."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from contract_lab.readers import (
    additional_properties_control,
    as_strict,
    as_tolerant,
    project,
    validate,
)
from helpers import SCHEMA_ID, reading_schema


def _error(result, location):
    matches = [node for node in result["errors"] if node["instanceLocation"] == location]
    assert matches, result
    return matches[0]


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = reading_schema()
        self.known = {"gauge_id": "g-1", "height_mm": 1200}
        self.with_sibling = {**self.known, "spare_flag": True}

    def test_ref_property_is_accepted_by_strict_and_rejected_by_the_control(self) -> None:
        strict = validate(as_strict(self.schema), self.known)
        tolerant = validate(as_tolerant(self.schema), self.known)
        control = validate(additional_properties_control(self.schema), self.known)
        self.assertTrue(strict["valid"])
        self.assertTrue(tolerant["valid"])
        self.assertFalse(control["valid"])
        node = _error(control, "/gauge_id")
        self.assertEqual(node["keywordLocation"], "/additionalProperties")
        self.assertEqual(node["error"], "Additional property 'gauge_id' is not allowed.")
        self.assertEqual(
            node["absoluteKeywordLocation"],
            f"{SCHEMA_ID}#/additionalProperties",
        )

    def test_unknown_sibling_splits_strict_and_tolerant(self) -> None:
        strict = validate(as_strict(self.schema), self.with_sibling)
        tolerant = validate(as_tolerant(self.schema), self.with_sibling)
        self.assertTrue(tolerant["valid"])
        self.assertFalse(strict["valid"])
        node = _error(strict, "/spare_flag")
        self.assertEqual(node["keywordLocation"], "/unevaluatedProperties")
        self.assertEqual(
            node["absoluteKeywordLocation"],
            f"{SCHEMA_ID}#/unevaluatedProperties",
        )
        self.assertEqual(node["instanceLocation"], "/spare_flag")
        self.assertEqual(node["error"], "Unevaluated property 'spare_flag' is not allowed.")

    def test_pointer_escapes_a_slash_in_the_sibling_name(self) -> None:
        payload = {**self.known, "tide/note": "west"}
        result = validate(as_strict(self.schema), payload)
        node = _error(result, "/tide~1note")
        self.assertEqual(node["keywordLocation"], "/unevaluatedProperties")

    def test_if_then_annotations_are_applied_before_unevaluated_properties(self) -> None:
        schema = {
            "$id": SCHEMA_ID,
            "type": "object",
            "if": {
                "required": ["kind"],
                "properties": {"kind": {"type": "string"}},
            },
            "then": {
                "properties": {
                    "kind": {"type": "string"},
                    "height_mm": {"type": "integer"},
                }
            },
            "else": {
                "properties": {
                    "note": {"type": "string"},
                }
            },
            "unevaluatedProperties": False,
        }
        taken = validate(schema, {"kind": "tide", "height_mm": 1})
        other_branch = validate(schema, {"kind": "tide", "note": "west"})
        else_branch = validate(schema, {"note": "west"})
        self.assertTrue(taken["valid"])
        self.assertFalse(other_branch["valid"])
        self.assertEqual(_error(other_branch, "/note")["keywordLocation"], "/unevaluatedProperties")
        self.assertTrue(else_branch["valid"])

    def test_pattern_and_additional_annotations_precede_unevaluated_properties(self) -> None:
        patterned = {
            "$id": SCHEMA_ID,
            "type": "object",
            "properties": {"gauge_id": {"type": "string"}},
            "patternProperties": {"^ext_": {"type": "string"}},
            "unevaluatedProperties": False,
        }
        self.assertTrue(validate(patterned, {"gauge_id": "g-1", "ext_note": "a"})["valid"])
        rejected = validate(patterned, {"gauge_id": "g-1", "spare_flag": "a"})
        self.assertEqual(_error(rejected, "/spare_flag")["instanceLocation"], "/spare_flag")
        closed_by_additional = {
            "$id": SCHEMA_ID,
            "type": "object",
            "properties": {"gauge_id": {"type": "string"}},
            "additionalProperties": {"type": "string"},
            "unevaluatedProperties": False,
        }
        self.assertTrue(
            validate(closed_by_additional, {"gauge_id": "g-1", "extra": "ok"})["valid"]
        )

    def test_boolean_and_float_are_not_integers(self) -> None:
        schema = as_strict(self.schema)
        self.assertFalse(validate(schema, {"gauge_id": "g-1", "height_mm": True})["valid"])
        self.assertFalse(validate(schema, {"gauge_id": "g-1", "height_mm": 1.0})["valid"])
        self.assertTrue(validate(schema, self.known)["valid"])

    def test_projection_uses_evaluated_names_only(self) -> None:
        schema = {
            "type": "object",
            "allOf": [{"$ref": "#/$defs/core"}],
            "$defs": {
                "core": {"properties": {"gauge_id": {"type": "string"}}},
                "unused": {"properties": {"operator_note": {"type": "string"}}},
            },
            "if": {"required": ["kind"], "properties": {"kind": {"type": "string"}}},
            "then": {"properties": {"height_mm": {"type": "integer"}}},
            "else": {"properties": {"note": {"type": "string"}}},
        }
        payload = {"gauge_id": "g-1", "kind": "tide", "height_mm": 3, "note": "x", "operator_note": "y"}
        self.assertEqual(
            project(schema, payload),
            {"gauge_id": "g-1", "kind": "tide", "height_mm": 3},
        )
        self.assertEqual(project(schema, {"gauge_id": "g-1", "note": "x"}), {"gauge_id": "g-1", "note": "x"})

    def test_strict_copy_keeps_a_schema_valued_additional_properties(self) -> None:
        schema = {
            "type": "object",
            "properties": {"gauge_id": {"type": "string"}},
            "additionalProperties": {"type": "string"},
        }
        strict = as_strict(schema)
        self.assertEqual(strict["additionalProperties"], {"type": "string"})
        self.assertTrue(validate(strict, {"gauge_id": "g-1", "extra": "ok"})["valid"])
        node = _error(validate(strict, {"gauge_id": "g-1", "extra": 1}), "/extra")
        self.assertEqual(node["keywordLocation"], "/additionalProperties/type")
        self.assertNotIn("additionalProperties", as_strict({**schema, "additionalProperties": False}))
