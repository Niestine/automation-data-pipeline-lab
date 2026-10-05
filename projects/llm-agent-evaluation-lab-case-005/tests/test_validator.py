"""Schema admission, format annotation, and the Draft 2020-12 subset."""

from __future__ import annotations

import unittest

from helpers import EXAMPLES, outcome
from repair_gate.harness import CassetteProvider, EpisodeConfig, run_episode
from repair_gate.util import load_json
from repair_gate.validator import admit, validate


class ValidatorTests(unittest.TestCase):
    def test_schema_suite_matches_every_fixture_flag(self):
        cases = load_json(EXAMPLES / "schema_suite.json")
        for case in cases:
            for item in case["tests"]:
                with self.subTest(schema=case["id"], description=item["description"]):
                    verdict = validate(case["schema"], item["data"], format_assertion=True)
                    self.assertEqual(verdict.valid, item["valid"])
                    self.assertEqual(verdict.flag["valid"], item["valid"])
        coverage = outcome()["report"]["coverage"]
        self.assertEqual(coverage["accuracy"], 1.0)
        engines = coverage["engines"]
        for name in ("sch-format", "sch-pattern", "sch-minimum", "sch-unique"):
            self.assertEqual(engines[name]["strict_mask"]["declared"], 1)
            self.assertEqual(engines[name]["strict_mask"]["empirical"], 0.5)
            self.assertEqual(engines[name]["strict_mask"]["compliance"], 0.5)
            self.assertEqual(engines[name]["closed_grammar"]["declared"], 0)
            self.assertIsNone(engines[name]["closed_grammar"]["compliance"])
        for name in ("sch-enum", "sch-const", "sch-required", "sch-additional", "sch-type"):
            self.assertEqual(engines[name]["strict_mask"]["declared"], 1)
            self.assertEqual(engines[name]["strict_mask"]["empirical"], 1.0)
            self.assertEqual(engines[name]["closed_grammar"]["declared"], 1)
        for name in ("sch-allof", "sch-if"):
            self.assertEqual(engines[name]["strict_mask"]["declared"], 0)
            self.assertIsNone(engines[name]["strict_mask"]["compliance"])
            self.assertIsNone(engines[name]["closed_grammar"]["compliance"])

    def test_format_is_an_annotation_unless_assertion_is_on(self):
        schema = {"type": "string", "format": "email"}
        off = validate(schema, "not-an-email")
        on = validate(schema, "not-an-email", format_assertion=True)
        self.assertTrue(off.valid)
        self.assertFalse(on.valid)
        self.assertFalse(off.annotations[0]["asserted"])
        self.assertTrue(on.annotations[0]["asserted"])
        unknown = {"type": "string", "format": "kiln-dock"}
        held = validate(unknown, "whatever", format_assertion=True)
        self.assertTrue(held.valid)
        self.assertFalse(held.annotations[0]["asserted"])

    def test_candidate_valid_and_confidence_are_not_the_verdict(self):
        verdict = validate({"type": "object"}, {"valid": False, "confidence": 0.1})
        self.assertTrue(verdict.valid)
        self.assertTrue(verdict.flag["valid"])

    def test_ref_error_uses_the_target_pointer(self):
        schema = {
            "$id": "https://example.test/pair",
            "$defs": {"code": {"type": "string", "enum": ["RQ-14"]}},
            "type": "object",
            "additionalProperties": False,
            "required": ["code"],
            "properties": {"code": {"$ref": "#/$defs/code"}},
        }
        verdict = validate(schema, {"code": "nope"})
        self.assertFalse(verdict.valid)
        error = verdict.errors[0]
        self.assertEqual(error["keyword"], "enum")
        self.assertEqual(error["keywordLocation"], "/$defs/code/enum")
        self.assertEqual(
            error["absoluteKeywordLocation"],
            "https://example.test/pair#/$defs/code/enum",
        )
        self.assertEqual(verdict.basic[0]["keywordLocation"], "/$defs/code/enum")

    def test_contract_maximum_is_a_separate_gate(self):
        schema = {"type": "object", "properties": {"amount": {"type": "integer"}}}
        verdict = validate(schema, {"amount": 9}, contracts=[{"pointer": "/amount", "maximum": 5}])
        self.assertEqual(verdict.errors[0]["keyword"], "x-contract")
        self.assertEqual(verdict.errors[0]["bound"], 5)

    def test_composition_never_reaches_strict_mode(self):
        schema = {"allOf": [{"type": "object", "additionalProperties": False, "properties": {}}]}
        admission = admit(schema)
        self.assertEqual(admission.declared_strict, 0)
        self.assertTrue(admission.strict_rejected)
        case = {
            "id": "strict-skip",
            "mode": "strict",
            "schema": schema,
            "channel": "text.format",
            "execute": False,
            "task": "skip",
            "context": "",
            "scripts": {"strict": [{"status": "object", "tokens": 1, "body": {}}]},
        }
        result = run_episode(case, CassetteProvider(case), EpisodeConfig())
        self.assertEqual(result.status, "strict_rejected")
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(result.prompts, [])

    def test_open_objects_fail_strict_admission(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
        }
        admission = admit(schema)
        self.assertEqual(admission.declared_strict, 0)
        self.assertEqual(admission.declared_closed, 0)
        problems = {item["problem"] for item in admission.object_problems}
        self.assertIn("additionalProperties", problems)
        self.assertIn("required", problems)

    def test_bare_and_referenced_objects_are_linted(self):
        bare = admit({"type": "object"})
        self.assertEqual(bare.declared_strict, 0)
        self.assertEqual(bare.object_problems, [{"location": "/", "problem": "additionalProperties"}])
        self.assertEqual(admit({"type": "object", "additionalProperties": False}).declared_strict, 1)
        referenced = admit(
            {
                "$defs": {"inner": {"type": "object", "properties": {"a": {"type": "string"}}}},
                "type": "object",
                "additionalProperties": False,
                "required": ["inner"],
                "properties": {"inner": {"$ref": "#/$defs/inner"}},
            }
        )
        self.assertEqual(referenced.declared_strict, 0)
        self.assertEqual(
            referenced.object_problems,
            [
                {"location": "/$defs/inner", "problem": "additionalProperties"},
                {"location": "/$defs/inner", "problem": "required"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
