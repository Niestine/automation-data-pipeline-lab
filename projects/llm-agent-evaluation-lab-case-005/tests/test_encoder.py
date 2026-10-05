"""Failure records copy only what the failing keyword enumerates."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401  (puts src on sys.path)
from repair_gate.encoder import ALTERNATIVE_CAP, encode_with_schema, render_repair
from repair_gate.validator import validate


class EncoderTests(unittest.TestCase):
    def test_enum_cap_keeps_schema_order(self):
        values = [f"v{index}" for index in range(13)]
        schema = {"enum": values}
        record = encode_with_schema(validate(schema, "other"), "other", schema)
        self.assertEqual(record["alternatives"], values[:ALTERNATIVE_CAP])
        self.assertEqual(len(record["alternatives"]), 12)
        self.assertEqual(record["alternatives_status"], "enumerated")

    def test_pattern_and_format_stay_unenumerated(self):
        pattern = {"type": "string", "pattern": "^RQ-"}
        record = encode_with_schema(validate(pattern, "nope"), "nope", pattern)
        self.assertEqual(record["label"], "pattern")
        self.assertEqual(record["alternatives"], [])
        self.assertEqual(record["alternatives_status"], "unenumerated")
        email = {"type": "string", "format": "email"}
        formatted = encode_with_schema(
            validate(email, "not-an-email", format_assertion=True),
            "not-an-email",
            email,
        )
        self.assertEqual(formatted["alternatives"], [])
        self.assertEqual(formatted["alternatives_status"], "unenumerated")

    def test_minimum_is_a_bound_not_a_guess(self):
        schema = {"type": "integer", "minimum": 1}
        record = encode_with_schema(validate(schema, 0), 0, schema)
        self.assertEqual(record["alternatives"], [{"minimum": 1}])
        self.assertNotIn(1, record["alternatives"])

    def test_required_names_are_the_alternatives(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["code", "lane"],
            "properties": {"code": {"type": "string"}, "lane": {"type": "string"}},
        }
        instance = {"code": "RQ-14"}
        record = encode_with_schema(validate(schema, instance), instance, schema)
        self.assertEqual(record["label"], "required")
        self.assertEqual(record["alternatives"], ["lane"])

    def test_ablation_views_share_the_same_three_facts(self):
        record = {
            "label": "enum",
            "location": "/code",
            "observed": "RQ-99",
            "alternatives": ["RQ-14", "RQ-22"],
            "alternatives_status": "enumerated",
            "raw_text": 'enum failed at /code observed "RQ-99"',
        }
        self.assertIsNone(render_repair(None, "raw"))
        self.assertEqual(render_repair(record, "raw"), record["raw_text"])
        loc = render_repair(record, "loc_obs")
        self.assertEqual(set(loc), {"label", "location", "observed"})
        prose = render_repair(record, "full_prose")
        keyed = render_repair(record, "full_keyed")
        self.assertIn("/code", prose)
        self.assertIn('"RQ-99"', prose)
        self.assertIn('["RQ-14","RQ-22"]', prose)
        self.assertEqual(keyed["location"], loc["location"])
        self.assertEqual(keyed["observed"], loc["observed"])
        self.assertEqual(keyed["alternatives"], ["RQ-14", "RQ-22"])
        self.assertEqual(
            prose,
            'Failure enum at /code. Observed "RQ-99". Alternatives: ["RQ-14","RQ-22"].',
        )


if __name__ == "__main__":
    unittest.main()
