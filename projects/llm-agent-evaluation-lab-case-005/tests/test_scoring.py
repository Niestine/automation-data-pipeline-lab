"""Leaf accuracy, LCS collateral, and hardened schema failures."""

from __future__ import annotations

import unittest

from helpers import EXAMPLES, outcome, result
from repair_gate.scoring import collateral, score_candidate, type_safety
from repair_gate.suite import _score_result
from repair_gate.util import load_json


class ScoringTests(unittest.TestCase):
    def test_removing_index_zero_does_not_tax_later_items(self):
        pre = {"items": ["a", "b", "c"]}
        post = {"items": ["b", "c"]}
        ops = [
            {"op": "test", "path": "/items/0", "value": "a"},
            {"op": "remove", "path": "/items/0"},
        ]
        self.assertEqual(collateral(pre, post, ops), 0)

    def test_index_zero_does_not_cover_index_one(self):
        pre = {"items": ["a", "b", "c"]}
        post = {"items": ["a", "X", "c"]}
        ops = [{"op": "replace", "path": "/items/0", "value": "Z"}]
        # A scalar swap is a deletion plus an insertion, both at /items/1.
        self.assertEqual(collateral(pre, post, ops), 2)
        self.assertEqual(
            collateral(pre, post, [{"op": "replace", "path": "/items/1", "value": "X"}]),
            0,
        )
        changed_only_zero = {"items": ["Z", "b", "c"]}
        self.assertEqual(
            collateral(pre, changed_only_zero, [{"op": "replace", "path": "/items/0", "value": "Z"}]),
            0,
        )

    def test_shipped_score_rows(self):
        report = outcome()["report"]
        self.assertEqual(report["harden"]["value_accuracy"], 0.0)
        self.assertEqual(report["harden"]["faithfulness"], 0.0)
        self.assertEqual(report["harden"]["structure_coverage"], 1.0)
        self.assertFalse(report["harden"]["schema_valid"])
        self.assertEqual(report["synonym"]["value_accuracy"], 0.5)
        self.assertEqual(report["synonym"]["faithfulness"], 0.5)
        self.assertFalse(report["synonym"]["perfect_response"])
        self.assertTrue(report["synonym"]["schema_valid"])
        self.assertEqual(report["extra"]["value_accuracy"], 1.0)
        self.assertEqual(report["extra"]["faithfulness"], 0.5)
        self.assertFalse(report["extra"]["perfect_response"])
        compare = report["regen_vs_patch"]
        self.assertEqual(compare["patch_tokens"], 46)
        self.assertEqual(compare["regen_tokens"], 170)
        self.assertEqual(compare["patch_collateral"], 0)
        # Regeneration fixed /arguments/currency as asked and also rewrote /arguments/note.
        self.assertEqual(compare["regen_collateral"], 1)
        self.assertTrue(compare["patch_final_object_match"])
        self.assertFalse(compare["regen_final_object_match"])

    def test_regeneration_collateral_excludes_only_the_failing_location(self):
        regen = result("ep-regen")
        self.assertEqual(regen.regen_target, "/arguments/currency")
        self.assertEqual(regen.ops_applied, [])
        pre = regen.pre_patch
        post = regen.candidate
        self.assertEqual(collateral(pre, post, []), 2)
        self.assertEqual(collateral(pre, post, [], ["/arguments/currency"]), 1)
        self.assertIsNone(result("ep-patch").regen_target)

    def test_exact_patch_is_separate_from_the_final_object(self):
        # Re-score from the frozen episode so exact-patch stays beside object match.
        schemas = load_json(EXAMPLES / "schemas.json")
        gold_file = load_json(EXAMPLES / "gold.json")["cases"]
        episodes = {item["id"]: item for item in load_json(EXAMPLES / "episodes.json")["cases"]}
        for case_id, expect_patch, expect_object in (
            ("ep-patch", True, True),
            ("ep-regen", False, False),
        ):
            episode = result(case_id)
            case = dict(episodes[case_id])
            case["schema"] = schemas[case["schema_id"]]
            score = _score_result(episode, gold_file[case_id], case["context"], case["schema"])
            self.assertEqual(score["exact_patch_match"], expect_patch)
            self.assertEqual(score["final_object_match"], expect_object)

    def test_schema_failure_zeroes_leaf_credit(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        }
        predicted = {"answer": "42", "confidence": 0.2}
        score = score_candidate(
            predicted,
            {"answer": "42"},
            schema=schema,
            schema_valid=False,
            parse_valid=True,
            context="the answer is 42",
            answer_pointer="/answer",
            gold_answer="42",
        )
        self.assertEqual(score["value_accuracy"], 0.0)
        self.assertEqual(score["faithfulness"], 0.0)
        self.assertTrue(score["task_exact_match"])
        self.assertFalse(score["format_success"])

    def test_boolean_is_not_an_integer_leaf(self):
        schema = {
            "type": "object",
            "properties": {"amount": {"type": "integer"}},
            "required": ["amount"],
        }
        self.assertEqual(type_safety(schema, {"amount": True}), 0.0)
        self.assertEqual(type_safety(schema, {"amount": 1}), 1.0)


if __name__ == "__main__":
    unittest.main()
