import json
import unittest

import helpers
from llm_agent_lab.contracts import (
    AGENT_OUTPUT_SCHEMA,
    ContractError,
    extract_json_object,
    parse_agent_output,
    validate_schema,
)


class ContractTests(unittest.TestCase):
    def test_valid_output_parses(self):
        output = parse_agent_output(helpers.valid_output_json())
        self.assertEqual(output.intent, "status_lookup")
        self.assertEqual(output.proposed_action, "lookup_record")
        self.assertFalse(output.needs_human)

    def test_markdown_fence_is_stripped(self):
        fenced = "```json\n" + helpers.valid_output_json() + "\n```"
        output = parse_agent_output(fenced)
        self.assertEqual(output.intent, "status_lookup")

    def test_surrounding_prose_still_extracts_object(self):
        blob = "here you go\n" + helpers.valid_output_json() + "\nthanks"
        extracted = extract_json_object(blob)
        output = parse_agent_output(extracted)
        self.assertEqual(output.confidence, 0.9)

    def test_empty_response_is_parse_error(self):
        with self.assertRaises(ContractError) as ctx:
            parse_agent_output("   ")
        self.assertEqual(ctx.exception.code, "parse_error")

    def test_non_json_is_parse_error(self):
        with self.assertRaises(ContractError) as ctx:
            parse_agent_output("{not json")
        self.assertEqual(ctx.exception.code, "parse_error")

    def test_extra_property_is_rejected(self):
        payload = helpers.valid_output_dict(extra="nope")
        with self.assertRaises(ContractError) as ctx:
            parse_agent_output(json.dumps(payload))
        self.assertEqual(ctx.exception.code, "schema_error")
        self.assertTrue(any("additional property" in err for err in ctx.exception.errors))

    def test_missing_rationale_is_rejected(self):
        payload = helpers.valid_output_dict()
        del payload["rationale"]
        with self.assertRaises(ContractError) as ctx:
            parse_agent_output(json.dumps(payload))
        self.assertEqual(ctx.exception.code, "schema_error")
        self.assertTrue(any("rationale" in err for err in ctx.exception.errors))

    def test_confidence_above_one_is_rejected(self):
        payload = helpers.valid_output_dict(confidence=1.2)
        errors = validate_schema(AGENT_OUTPUT_SCHEMA, payload)
        self.assertTrue(any("maximum" in err for err in errors))

    def test_confidence_integer_is_accepted_as_number(self):
        payload = helpers.valid_output_dict(confidence=1)
        self.assertEqual(validate_schema(AGENT_OUTPUT_SCHEMA, payload), [])

    def test_needs_human_must_be_boolean_not_integer(self):
        payload = helpers.valid_output_dict(needs_human=1)
        errors = validate_schema(AGENT_OUTPUT_SCHEMA, payload)
        self.assertTrue(any("needs_human" in err for err in errors))

    def test_unknown_intent_is_rejected(self):
        payload = helpers.valid_output_dict(intent="delete_everything")
        errors = validate_schema(AGENT_OUTPUT_SCHEMA, payload)
        self.assertTrue(any("not an allowed value" in err for err in errors))

    def test_empty_rationale_fails_min_length(self):
        payload = helpers.valid_output_dict(rationale="")
        errors = validate_schema(AGENT_OUTPUT_SCHEMA, payload)
        self.assertTrue(any("minLength" in err for err in errors))


    def test_nan_confidence_is_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            parse_agent_output(helpers.valid_output_json().replace("0.9", "NaN"))
        self.assertEqual(ctx.exception.code, "parse_error")

    def test_non_finite_number_fails_schema(self):
        payload = helpers.valid_output_dict(confidence=float("inf"))
        errors = validate_schema(AGENT_OUTPUT_SCHEMA, payload)
        self.assertTrue(any("finite" in err for err in errors))

    def test_unterminated_fence_still_extracts(self):
        output = parse_agent_output("```json\n" + helpers.valid_output_json())
        self.assertEqual(output.proposed_action, "lookup_record")

    def test_wrong_type_for_entities_is_rejected(self):
        payload = helpers.valid_output_dict(entities=["ORD-100"])
        with self.assertRaises(ContractError) as ctx:
            parse_agent_output(json.dumps(payload))
        self.assertTrue(any("entities" in err for err in ctx.exception.errors))


if __name__ == "__main__":
    unittest.main()
