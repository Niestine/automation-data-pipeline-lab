"""Strict dialect, format assertion, and the separate allowlist."""

from __future__ import annotations

import json
import unittest

from helpers import load_world

from office_gate_lab.approval import validate_elicitation_schema
from office_gate_lab.envelopes import PLAN_SCHEMA, SEND_ARGUMENT_SCHEMA
from office_gate_lab.errors import SchemaAdmissionError
from office_gate_lab.policy import Bound, allow_send
from office_gate_lab.schema_dialect import admit, validate_instance
from office_gate_lab.tools import call_tool, extract_fields, is_mutating
from office_gate_lab.world import ATTACKER, INJECT_TEXT, World


class SchemaDialectTests(unittest.TestCase):
    def test_min_length_and_max_length_are_rejected(self) -> None:
        for keyword in ("minLength", "maxLength"):
            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {"name": {"type": "string", keyword: 1}},
            }
            with self.assertRaises(SchemaAdmissionError):
                admit(schema)

    def test_extra_property_fails_a_closed_plan(self) -> None:
        plan = {
            "kind": "plan",
            "steps": [],
            "note": "extra",
        }
        errors = validate_instance(PLAN_SCHEMA, plan, format_assertion=False)
        self.assertTrue(any("additionalProperties" in error for error in errors))

    def test_format_assertion_is_syntactic_and_is_not_an_allow(self) -> None:
        payload = {"document_id": "doc-minutes", "recipient": "not-an-email"}
        self.assertEqual(validate_instance(SEND_ARGUMENT_SCHEMA, payload, format_assertion=False), [])
        self.assertTrue(validate_instance(SEND_ARGUMENT_SCHEMA, payload, format_assertion=True))
        valid_email = {"document_id": "doc-minutes", "recipient": ATTACKER}
        self.assertEqual(validate_instance(SEND_ARGUMENT_SCHEMA, valid_email, format_assertion=True), [])
        documents = {doc["id"]: doc for doc in load_world()["documents"]}
        readers = tuple(documents["doc-minutes"]["readers"])
        for recipient in ("not-an-email", ATTACKER):
            bound = Bound(recipient, False, ("call-1",), readers)
            allowed, reason = allow_send("doc-minutes", recipient, bound, documents)
            self.assertFalse(allowed)
            self.assertEqual(reason, "recipient_not_allowed")

    def test_elicitation_rejects_secrets_and_keeps_token_count(self) -> None:
        self.assertIsNone(
            validate_elicitation_schema(
                {
                    "type": "object",
                    "properties": {
                        "token_count": {"type": "integer", "description": "Count of items"},
                    },
                    "required": ["token_count"],
                }
            )
        )
        for name in ("password", "token", "secret"):
            schema = {
                "type": "object",
                "properties": {name: {"type": "string", "description": "A label"}},
                "required": [name],
            }
            self.assertEqual(validate_elicitation_schema(schema), "elicitation_secret")
        described = {
            "type": "object",
            "properties": {"label": {"type": "string", "description": "Enter the secret"}},
            "required": ["label"],
        }
        self.assertEqual(validate_elicitation_schema(described), "elicitation_secret")
        nested = {
            "type": "object",
            "properties": {"label": {"type": "object", "properties": {}}},
            "required": ["label"],
        }
        self.assertEqual(validate_elicitation_schema(nested), "elicitation_nested")
        non_object = {"type": "object", "properties": {"label": "string"}, "required": ["label"]}
        self.assertEqual(validate_elicitation_schema(non_object), "elicitation_not_primitive")

    def test_read_only_hint_does_not_decide_mutability(self) -> None:
        self.assertTrue(is_mutating("send_document", {"annotations": {"readOnlyHint": True}}))
        self.assertFalse(is_mutating("read_document", {"annotations": {"readOnlyHint": False}}))

    def test_extractor_copies_declared_fields_only(self) -> None:
        world = World(load_world())
        result = call_tool(
            "read_document",
            {"document_id": "doc-minutes"},
            world,
            attack=True,
            attack_mode="overwrite_named_recipient",
            replies={},
        )
        structured = result["structuredContent"]
        self.assertIn(INJECT_TEXT, structured["body"])
        self.assertIn(ATTACKER, structured["readers"])
        self.assertEqual(structured["named_recipient"], ATTACKER)
        extracted = extract_fields(structured)
        blob = json.dumps(extracted)
        self.assertNotIn("export_all", blob)
        self.assertNotIn("injected", blob)
        self.assertEqual(extracted["fields"]["named_recipient"], ATTACKER)
        self.assertNotIn("readers", extracted["fields"])
        self.assertEqual(world.outbox, [])


if __name__ == "__main__":
    unittest.main()
