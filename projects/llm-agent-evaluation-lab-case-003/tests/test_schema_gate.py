"""Closed cited_answer_v1 checks, bound checks, and the schema retry budget."""

import unittest

import helpers
from rag_eval_lab.generator import SequenceGenerator
from rag_eval_lab.pipeline import RunConfig
from rag_eval_lab.retry import backoff_seconds
from rag_eval_lab.schema_gate import DIALECT, RESPONSE_SCHEMA, citation_bound_errors, validate, validate_response

from helpers import envelope, fresh_lab


class SchemaGateTests(unittest.TestCase):
    def test_dialect_and_closed_object(self) -> None:
        self.assertEqual(DIALECT, "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(RESPONSE_SCHEMA["$schema"], DIALECT)
        self.assertIs(RESPONSE_SCHEMA["additionalProperties"], False)
        self.assertEqual(RESPONSE_SCHEMA["minProperties"], 5)
        self.assertEqual(RESPONSE_SCHEMA["maxProperties"], 5)
        self.assertEqual(len(RESPONSE_SCHEMA["required"]), 5)

    def test_extra_key_missing_key_and_bad_enum(self) -> None:
        payload = envelope("q-gate", [])
        payload["extra"] = 1
        self.assertTrue(any("additionalProperties" in error for error in validate_response(payload)))
        del payload["extra"]
        del payload["surface_text"]
        self.assertTrue(any("surface_text" in error and "required" in error for error in validate_response(payload)))
        bad = envelope(
            "q-gate",
            [
                {
                    "claim_id": "G-GATE",
                    "text": "The Harborline Depot north gate code is 4419.",
                    "kind": "fact",
                    "support": "yes",
                    "citations": [],
                    "fallback": None,
                }
            ],
        )
        self.assertTrue(any("support" in error and "enum" in error for error in validate_response(bad)))

    def test_negative_offset_bool_and_null_claim_id(self) -> None:
        citation = {"chunk_id": "ch-gate", "start": -1, "end": 4}
        errors = validate(citation, RESPONSE_SCHEMA["properties"]["claims"]["items"]["properties"]["citations"]["items"])
        self.assertTrue(any("minimum" in error for error in errors))
        flagged = {"chunk_id": "ch-gate", "start": True, "end": 1}
        errors = validate(flagged, RESPONSE_SCHEMA["properties"]["claims"]["items"]["properties"]["citations"]["items"])
        self.assertTrue(any("start" in error and "type" in error for error in errors))
        null_claim = {
            "claim_id": None,
            "text": "No planted id.",
            "kind": "insufficient_evidence",
            "support": "abstain",
            "citations": [],
            "fallback": None,
        }
        self.assertEqual(
            validate(null_claim, RESPONSE_SCHEMA["properties"]["claims"]["items"]),
            [],
        )

    def test_nested_fallback_is_a_closed_object(self) -> None:
        claim_schema = RESPONSE_SCHEMA["properties"]["claims"]["items"]
        statement = {"text": "x", "explanation": "y", "verdict": "no", "chunk_id": None}
        base = {
            "claim_id": None,
            "text": "Northwind Tire holds the tire contract.",
            "kind": "fact",
            "support": "unsupported",
            "citations": [],
            "fallback": {"statements": [statement]},
        }
        self.assertEqual(validate(base, claim_schema), [])
        extra = dict(base, fallback={"statements": [statement], "score": 0.9})
        self.assertTrue(any("fallback.score" in error and "additionalProperties" in error for error in validate(extra, claim_schema)))
        missing = dict(base, fallback={"statements": [{"text": "x", "explanation": "y", "chunk_id": None}]})
        self.assertTrue(any("verdict" in error and "required" in error for error in validate(missing, claim_schema)))
        bad_verdict = dict(base, fallback={"statements": [dict(statement, verdict="maybe")]})
        self.assertTrue(any("verdict" in error and "enum" in error for error in validate(bad_verdict, claim_schema)))
        planted = envelope("q-gate", [dict(base, claim_id="G-GATE")])
        self.assertTrue(any("null fallback" in error for error in citation_bound_errors(planted, {"ch-gate": 44})))
        bare = envelope("q-vendor", [dict(base, fallback=None)])
        self.assertTrue(any("requires fallback" in error for error in citation_bound_errors(bare, {})))

    def test_format_is_ignored_and_end_before_start_is_a_bound_check(self) -> None:
        self.assertEqual(validate("not-a-date", {"type": "string", "format": "date"}), [])
        self.assertEqual(validate([1, "a"], {"prefixItems": [{"type": "integer"}, {"type": "string"}]}), [])
        citation = {"chunk_id": "ch-gate", "start": 5, "end": 2}
        schema = RESPONSE_SCHEMA["properties"]["claims"]["items"]["properties"]["citations"]["items"]
        self.assertEqual(validate(citation, schema), [])
        payload = envelope(
            "q-gate",
            [
                {
                    "claim_id": "G-GATE",
                    "text": "The Harborline Depot north gate code is 4419.",
                    "kind": "fact",
                    "support": "supported",
                    "citations": [citation],
                    "fallback": None,
                }
            ],
        )
        bounds = citation_bound_errors(payload, {"ch-gate": 44})
        self.assertTrue(any("end < start" in error for error in bounds))

    def test_schema_retry_then_success_uses_two_attempts(self) -> None:
        lab = fresh_lab()
        result = lab.run_item(lab.corpus.items["q-schema-retry"])
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["scored"])
        self.assertIsNotNone(result["metrics"])
        self.assertEqual(result["acceptance"], "require_approval")
        self.assertFalse(result["accurate"])
        self.assertEqual(len(lab.sleeper.delays), 1)
        self.assertEqual(lab.sleeper.delays[0], backoff_seconds(0, lab.seed, "q-schema-retry", 0.05))
        self.assertGreaterEqual(lab.sleeper.delays[0], 0.05)
        self.assertLessEqual(lab.sleeper.delays[0], 0.05 + 0.01)

    def test_exhausted_schema_budget_is_unscored(self) -> None:
        lab = fresh_lab()
        lab.generator = SequenceGenerator([{"schema_version": "cited_answer_v1", "extra": 1}] * 3)
        lab.generator.provider_id = "sequence"
        result = lab.run_item(lab.corpus.items["q-schema-retry"], RunConfig())
        self.assertEqual(result["attempts"], 3)
        self.assertFalse(result["scored"])
        self.assertIsNone(result["metrics"])
        self.assertFalse(result["accurate"])
        self.assertEqual(result["acceptance"], "reject")
        self.assertEqual(result["abstain_reason"], "schema_failure")
        self.assertEqual(len(lab.sleeper.delays), 2)
        self.assertGreater(lab.sleeper.delays[1], lab.sleeper.delays[0])
        self.assertEqual(lab.clock.now, sum(lab.sleeper.delays))

    def test_bound_error_retries_then_accepts(self) -> None:
        bad = envelope(
            "q-schema-retry",
            [
                {
                    "claim_id": "G-BELL",
                    "text": "The dock bell rings once at the start of shift.",
                    "kind": "fact",
                    "support": "supported",
                    "citations": [{"chunk_id": "ch-bell", "start": 5, "end": 2}],
                    "fallback": None,
                }
            ],
        )
        good = {
            "schema_version": "cited_answer_v1",
            "item_id": "q-schema-retry",
            "surface_text": "The handbook does not contain enough information to answer this question.",
            "reverse_questions": ["How does the dock bell mark the start of shift?"],
            "claims": [
                {
                    "claim_id": None,
                    "text": "The handbook does not contain enough information to answer this question.",
                    "kind": "insufficient_evidence",
                    "support": "abstain",
                    "citations": [],
                    "fallback": None,
                }
            ],
        }
        lab = fresh_lab()
        lab.generator = SequenceGenerator([bad, good])
        result = lab.run_item(lab.corpus.items["q-schema-retry"])
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["scored"])
        self.assertEqual(len(lab.sleeper.delays), 1)


if __name__ == "__main__":
    unittest.main()
