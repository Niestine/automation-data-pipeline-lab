"""Each seeded directional rule emits that id and no twin."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from contract_lab.changes import diff_openapi, impact_for
from contract_lab.compliance import score_history
from contract_lab.rules import OPTIONAL_RESPONSE_READ_ONLY, RULES, rule_by_id
from helpers import pair


def _shape(rule_id: str):
    side = "request" if rule_id.startswith("request") else "response"
    place = "body" if "-body-" in rule_id else "property"
    required = "-required-property-" in f"-{rule_id}"
    if "list-of-types-narrowed" in rule_id:
        before, after = {"type": ["string", "integer"]}, {"type": ["string"]}
    elif "list-of-types-widened" in rule_id:
        before, after = {"type": ["string"]}, {"type": ["string", "integer"]}
    elif "schema-became-not-false" in rule_id:
        before, after = False, {"type": "object"}
    elif "schema-became-false" in rule_id:
        before, after = {"type": "object"}, False
    elif rule_id.endswith("type-compatible"):
        before, after = {"type": "integer"}, {"type": "number"}
    elif rule_id.endswith("type-generalized"):
        before, after = {"type": "integer"}, {}
    elif rule_id.endswith("type-specialized"):
        before, after = {}, {"type": "integer"}
        place = "body"
    elif rule_id.endswith("content-encoding-changed"):
        before = {"type": "string", "contentEncoding": "base64"}
        after = {"type": "string", "contentEncoding": "base64url"}
        place = "property"
    elif rule_id.endswith("content-media-type-changed"):
        before = {"type": "string", "contentMediaType": "text/plain"}
        after = {"type": "string", "contentMediaType": "application/octet-stream"}
        place = "property"
    elif rule_id.endswith("type-changed"):
        before, after = {"type": "string"}, {"type": "integer"}
    elif "became-not-read-only" in rule_id:
        before, after = {"type": "integer", "readOnly": True}, {"type": "integer", "readOnly": False}
        place = "property"
    elif "became-read-only" in rule_id:
        before, after = {"type": "integer", "readOnly": False}, {"type": "integer", "readOnly": True}
        place = "property"
    elif "became-not-write-only" in rule_id:
        before = {"type": "integer", "writeOnly": True}
        after = {"type": "integer", "writeOnly": False}
        place = "property"
    elif "became-write-only" in rule_id:
        before = {"type": "integer", "writeOnly": False}
        after = {"type": "integer", "writeOnly": True}
        place = "property"
    else:
        raise AssertionError(rule_id)
    if place == "property":
        def wrap(schema, with_required):
            body = {"type": "object", "properties": {"height_mm": schema}}
            if with_required:
                body["required"] = ["height_mm"]
            return body
        before = wrap(before, required)
        after = wrap(after, required)
    return before, after, side


class RuleIdTests(unittest.TestCase):
    def test_every_seeded_fixture_emits_exactly_its_id(self) -> None:
        self.assertEqual(len(RULES), 32)
        for row in RULES:
            before, after, side = _shape(row["id"])
            left, right = pair(before, after, side=side)
            self.assertEqual(diff_openapi(left, right), [row["id"]], row["id"])

    def test_widening_does_not_emit_the_narrowed_or_request_id(self) -> None:
        before, after, side = _shape("response-body-list-of-types-widened")
        ids = diff_openapi(*pair(before, after, side=side))
        self.assertEqual(ids, ["response-body-list-of-types-widened"])
        self.assertNotIn("response-body-list-of-types-narrowed", ids)
        self.assertFalse(any(item.startswith("request-") for item in ids))

    def test_request_content_encoding_does_not_emit_the_response_twin(self) -> None:
        before, after, side = _shape("request-property-content-encoding-changed")
        ids = diff_openapi(*pair(before, after, side=side))
        self.assertEqual(ids, ["request-property-content-encoding-changed"])
        self.assertNotIn("response-property-content-encoding-changed", ids)

    def test_optional_read_only_id_is_the_recovered_spelling(self) -> None:
        self.assertEqual(OPTIONAL_RESPONSE_READ_ONLY, "response-optional-property-became-read-only")
        self.assertNotIn("tional-property-became-read-only", {row["id"] for row in RULES})
        row = rule_by_id(OPTIONAL_RESPONSE_READ_ONLY)
        self.assertEqual(row["location"], "response")
        self.assertEqual(row["axis"], "mutability")
        self.assertEqual(row["level"], "Info")

    def test_transcribed_levels_do_not_set_compliance(self) -> None:
        self.assertEqual(rule_by_id("response-body-list-of-types-narrowed")["level"], "Info")
        self.assertEqual(rule_by_id("response-body-list-of-types-widened")["level"], "Breaking")
        self.assertEqual(rule_by_id("request-body-type-changed")["level"], "Breaking")
        self.assertEqual(impact_for("response-body-list-of-types-narrowed", "tolerant"), "undecidable")
        self.assertEqual(impact_for("response-body-list-of-types-widened", "strict"), "undecidable")
        before, after, side = _shape("response-body-list-of-types-narrowed")
        release = {
            "before": pair(before, after, side=side)[0],
            "after": pair(before, after, side=side)[1],
            "before_version": "1.0.0",
            "after_version": "1.1.0",
        }
        self.assertEqual(score_history([release], "strict"), "undecidable")
        self.assertEqual(score_history([release], "tolerant"), "undecidable")
        widened = _shape("response-body-list-of-types-widened")
        widened_release = {
            "before": pair(widened[0], widened[1], side=widened[2])[0],
            "after": pair(widened[0], widened[1], side=widened[2])[1],
            "before_version": "1.0.0",
            "after_version": "1.1.0",
        }
        self.assertEqual(score_history([widened_release], "strict"), "undecidable")
        self.assertEqual(score_history([widened_release], "tolerant"), "undecidable")
