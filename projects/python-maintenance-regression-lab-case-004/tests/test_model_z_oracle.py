"""A cut inside a commit record keeps the previous commit and does not raise."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from recovery_lab.metrics import commit_cut_balances, two_commit_trace
from recovery_lab.outcomes import DURABLE_MATCH


class ModelZOracleTests(unittest.TestCase):
    def test_partial_commit_record_absent(self) -> None:
        trace = two_commit_trace(publish_before_log_flush=False)
        for block in (512, 4096):
            rows = commit_cut_balances(trace, block, 1)
            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertEqual(row["outcome"], DURABLE_MATCH)
                self.assertEqual(row["balance"], 10)
                self.assertGreaterEqual(row["offset"], 0)
