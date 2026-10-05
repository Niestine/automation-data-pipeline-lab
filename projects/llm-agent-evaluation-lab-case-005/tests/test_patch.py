"""RFC 6902 apply and the contract mask, driven by examples/patch_suite.json."""

from __future__ import annotations

import unittest
from copy import deepcopy

from helpers import EXAMPLES
from repair_gate.patch import apply_patch, mask_patch
from repair_gate.util import json_equal, load_json


class PatchSuiteTests(unittest.TestCase):
    def test_fixture_layers(self):
        for case in load_json(EXAMPLES / "patch_suite.json"):
            with self.subTest(case=case["name"]):
                original = deepcopy(case["doc"])
                if case["layer"] == "apply":
                    result = apply_patch(case["doc"], case["ops"])
                    self.assertEqual(result.ok, case["ok"])
                    self.assertEqual(result.reason, case.get("reason"))
                    if case["ok"]:
                        self.assertTrue(json_equal(result.document, case["expect"]))
                        self.assertIsNot(result.document, case["doc"])
                    else:
                        self.assertIs(result.document, case["doc"])
                else:
                    masked = mask_patch(
                        case["ops"],
                        document=case["doc"],
                        repair=case.get("repair"),
                        allow_move_copy=bool(case.get("allow_move_copy", False)),
                        op_budget=case.get("op_budget", 2),
                    )
                    self.assertEqual(masked.ok, case["ok"])
                    self.assertEqual(masked.reason, case["reason"])
                self.assertEqual(case["doc"], original)

    def test_string_versus_number_stops_before_replace(self):
        document = {"a": 10}
        ops = [
            {"op": "test", "path": "/a", "value": "10"},
            {"op": "replace", "path": "/a", "value": 11},
        ]
        result = apply_patch(document, ops)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "test_mismatch")
        self.assertIs(result.document, document)
        self.assertEqual(document, {"a": 10})

    def test_numeric_one_matches_one_point_zero(self):
        document = {"a": 1}
        result = apply_patch(
            document,
            [
                {"op": "test", "path": "/a", "value": 1.0},
                {"op": "replace", "path": "/a", "value": 2},
            ],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.document, {"a": 2})
        self.assertEqual(document, {"a": 1})

    def test_boolean_is_not_a_number(self):
        result = apply_patch({"a": 1}, [{"op": "test", "path": "/a", "value": True}])
        self.assertEqual(result.reason, "test_mismatch")

    def test_extra_members_are_ignored_and_recorded(self):
        document = {"a": 1}
        ops = [{"op": "add", "path": "/b", "value": 2, "comment": "keep"}]
        masked = mask_patch(ops, document=document, repair=None)
        self.assertTrue(masked.ok)
        self.assertEqual(masked.ignored_members, [{"index": 0, "members": ["comment"]}])
        applied = apply_patch(document, ops)
        self.assertEqual(applied.document, {"a": 1, "b": 2})
        self.assertEqual(document, {"a": 1})


if __name__ == "__main__":
    unittest.main()
