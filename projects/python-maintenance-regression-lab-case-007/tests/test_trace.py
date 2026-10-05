"""Frozen correlated trace. The locked triple is this lab's measurement, not the preprint's."""

from __future__ import annotations

import json
import unittest

import helpers
from bay_notice.trace import compare

# Hand-checked against the doom-next rule in trace.py before the policies were scored.
LOCKED = {
    "no-retry": {"successes": 5, "calls": 8},
    "standard": {"successes": 3, "calls": 19},
    "budgeted": {"successes": 6, "calls": 9},
}


class TraceTests(unittest.TestCase):
    def test_standard_retry_loses_to_no_retry_and_the_budget_spends_less(self) -> None:
        raw = json.loads((helpers.EXAMPLES / "correlated_trace.json").read_text(encoding="utf-8"))
        scored = compare(raw["jobs"], raw["draws"], float(raw["base_load"]))
        for name, locked in LOCKED.items():
            self.assertEqual(scored[name]["successes"], locked["successes"], name)
            self.assertEqual(scored[name]["calls"], locked["calls"], name)
        self.assertLess(scored["standard"]["successes"], scored["no-retry"]["successes"])
        self.assertGreaterEqual(scored["budgeted"]["successes"], scored["no-retry"]["successes"])
        self.assertLess(scored["budgeted"]["raf"], scored["standard"]["raf"])
        self.assertEqual(scored["no-retry"]["success_rate"], 0.625)
        self.assertEqual(scored["standard"]["success_rate"], 0.375)
        self.assertEqual(scored["budgeted"]["success_rate"], 0.75)
        self.assertEqual(scored["budgeted"]["raf"], 1.125)
        self.assertEqual(scored["standard"]["raf"], 2.375)


if __name__ == "__main__":
    unittest.main()
