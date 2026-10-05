"""Full jitter stays inside the cap and beats synchronized backoff in this model."""

from __future__ import annotations

import random
import unittest

import helpers  # noqa: F401
from shiftlease.jitter import compare_cohorts, full_jitter, synchronized_delay


class JitterTests(unittest.TestCase):
    def test_full_jitter_is_inside_the_cap_and_not_constant(self) -> None:
        rng = random.Random(1)
        attempt = 3
        base = 0.05
        cap = 1.0
        ceiling = min(cap, base * (2**attempt))
        delays = [full_jitter(rng, attempt, base, cap) for _ in range(200)]
        self.assertTrue(all(0.0 <= delay <= ceiling for delay in delays))
        self.assertGreater(len({round(delay, 6) for delay in delays}), 1)
        self.assertEqual(synchronized_delay(attempt, base, cap), ceiling)
        self.assertEqual(full_jitter(rng, 0, 0.0, 0.0), 0.0)

    def test_cohort_records_fewer_failed_claims_and_a_shorter_drain(self) -> None:
        report = compare_cohorts(n_clients=20, n_jobs=20, seed=20261005)
        jitter = report["full_jitter"]
        synced = report["synchronized"]
        self.assertEqual(jitter["failed_claims"], 19)
        self.assertEqual(synced["failed_claims"], 57)
        self.assertEqual(synced["drain_time"], 5.0)
        self.assertLess(jitter["drain_time"], synced["drain_time"])
        self.assertLess(jitter["drain_time"], 1.0)
