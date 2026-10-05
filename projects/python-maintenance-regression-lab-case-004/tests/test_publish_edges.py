"""Model Z on the default publish order, and early detection of the missing flush edge."""

from __future__ import annotations

import math
import unittest

import helpers  # noqa: F401

from recovery_lab.faults import first_failure_rank
from recovery_lab.metrics import analyze, two_commit_trace
from recovery_lab.outcomes import DATA_LOSS, DURABLE_MATCH, MIXED_FIELDS


class PublishEdgeTests(unittest.TestCase):
    def test_default_protocol_model_z(self) -> None:
        trace = two_commit_trace(publish_before_log_flush=False)
        for block in (512, 4096):
            rows = analyze(trace, block)
            self.assertGreater(len(rows), 0)
            self.assertTrue(all(row["outcome"] == DURABLE_MATCH for row in rows))
            self.assertEqual(sum(1 for row in rows if row["outcome"] == MIXED_FIELDS), 0)

    def test_missing_flush_found_early(self) -> None:
        trace = two_commit_trace(publish_before_log_flush=True)
        rows = analyze(trace, 512)
        classes = [row["outcome"] for row in rows]
        rank = first_failure_rank(classes)
        limit = math.ceil(0.05 * len(rows))
        self.assertIsNotNone(rank)
        assert rank is not None
        self.assertLessEqual(rank, limit)
        self.assertIn(classes[rank - 1], (DATA_LOSS, MIXED_FIELDS))
        self.assertEqual(rows[rank - 1]["score"], 100)
        # The published snapshot carries the second commit while no commit
        # record reached a flush: the snapshot outran the log.
        self.assertEqual(rows[rank - 1]["balance"], 25)
        self.assertEqual(rows[rank - 1]["last_durable_lsn"], 0)
