"""Database-clock leases: honor, reclaim, uncertainty, early release, renewal."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import helpers


class LeaseTests(unittest.TestCase):
    def test_claim_honors_a_live_lease_and_reclaims_after_the_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = helpers.open_store(Path(tmp), lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            first = store.claim("holder-a", random.Random(1))
            self.assertEqual(first.kind, "job")
            self.assertEqual(first.fence, 1)
            self.assertEqual(first.attempts, 1)
            self.assertEqual(first.grant_seconds, 30)
            blocked = store.claim("holder-b", random.Random(2))
            self.assertEqual(blocked.kind, "empty")
            self.assertTrue(store.renew(first.job_id, "holder-a", first.fence))
            self.assertEqual(store.job(first.job_id)["attempts"], 1)
            self.assertEqual(store.sweep_dead(), [])
            store.shift_clock(31)
            self.assertFalse(store.renew(first.job_id, "holder-a", first.fence))
            second = store.claim("holder-b", random.Random(2))
            self.assertEqual(second.job_id, first.job_id)
            self.assertEqual(second.fence, 2)
            self.assertEqual(second.attempts, 2)
            self.assertEqual(store.job(first.job_id)["owner"], "holder-b")
            store.close()

    def test_backward_clock_keeps_the_stored_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = helpers.open_store(Path(tmp), lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            first = store.claim("holder-a", random.Random(1))
            store.shift_clock(-100)
            blocked = store.claim("holder-b", random.Random(2))
            self.assertEqual(blocked.kind, "empty")
            row = store.job(first.job_id)
            assert row is not None
            self.assertEqual(row["owner"], "holder-a")
            self.assertEqual(row["fence"], 1)
            self.assertGreater(row["lease_until"], store.clock_now())
            store.close()

    def test_uncertainty_stops_the_holder_before_the_stored_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = helpers.open_store(Path(tmp), lease_term_seconds=30, clock_uncertainty_seconds=10)
            store.submit("slip-1", helpers.slip())
            first = store.claim("holder-a", random.Random(1))
            self.assertTrue(store.holder_trusts(first.job_id, "holder-a", first.fence))
            store.shift_clock(25)
            self.assertFalse(store.holder_trusts(first.job_id, "holder-a", first.fence))
            blocked = store.claim("holder-b", random.Random(2))
            self.assertEqual(blocked.kind, "empty")
            self.assertEqual(store.job(first.job_id)["owner"], "holder-a")
            store.close()

    def test_early_release_allows_the_next_claim_inside_the_original_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = helpers.open_store(Path(tmp), lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            first = store.claim("holder-a", random.Random(1))
            self.assertEqual(store.claim("holder-b", random.Random(2)).kind, "empty")
            self.assertTrue(store.release(first.job_id, "holder-a", first.fence))
            self.assertEqual(store.job(first.job_id)["attempts"], 1)
            second = store.claim("holder-b", random.Random(2))
            self.assertEqual(second.kind, "job")
            self.assertEqual(second.fence, 2)
            self.assertEqual(second.attempts, 2)
            events = store.events(first.job_id)
            self.assertEqual([item["kind"] for item in events], ["claim", "release", "claim"])
            store.close()

    def test_lease_jitter_spreads_grant_lengths(self) -> None:
        from shiftlease.jitter import jitter_lease_term

        rng = random.Random(5)
        grants = {jitter_lease_term(rng, 4, 2) for _ in range(40)}
        self.assertTrue(grants <= {4, 5, 6})
        self.assertGreater(len(grants), 1)
