"""Expired claims spend the attempt budget. A live renewal does not."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

import helpers


class DeadLetterTests(unittest.TestCase):
    def test_repeated_expiry_dead_letters_at_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            term = 30
            store = helpers.open_store(root, lease_term_seconds=term, max_attempts=2)
            store.submit("slip-1", helpers.slip(), max_attempts=2)
            first = store.claim("holder-a", random.Random(1))
            self.assertTrue(store.record_error(first.job_id, "holder-a", first.fence, "boom-1"))
            store.shift_clock(term + 2)
            second = store.claim("holder-b", random.Random(2))
            self.assertEqual(second.attempts, 2)
            self.assertTrue(store.record_error(second.job_id, "holder-b", second.fence, "boom-2"))
            store.shift_clock((term + 2) * 2)
            self.assertEqual(store.sweep_dead(), [first.job_id])
            row = store.job(first.job_id)
            assert row is not None
            self.assertEqual(row["status"], "dead")
            self.assertEqual(row["last_error"], "boom-2")
            self.assertEqual(row["attempts"], 2)
            self.assertEqual(store.claim("holder-c", random.Random(3)).kind, "empty")
            kinds = [item["kind"] for item in store.events(first.job_id)]
            self.assertEqual(kinds[-1], "dead")
            store.close()

    def test_release_on_the_last_attempt_is_dead_lettered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = helpers.open_store(Path(tmp), lease_term_seconds=30, max_attempts=1)
            store.submit("slip-1", helpers.slip(), max_attempts=1)
            first = store.claim("holder-a", random.Random(1))
            self.assertTrue(store.release(first.job_id, "holder-a", first.fence))
            self.assertEqual(store.claim("holder-b", random.Random(2)).kind, "empty")
            self.assertEqual(store.sweep_dead(), [first.job_id])
            self.assertEqual(store.job(first.job_id)["status"], "dead")
            store.close()

    def test_drain_finishes_when_a_dead_job_keeps_a_pending_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            term = 4
            store = helpers.open_store(root, lease_term_seconds=term, effect_retry_cap=0, max_attempts=1)
            store.submit("slip-1", helpers.slip(), max_attempts=1)

            class Boom:
                def perform(self, **_kwargs: object) -> dict:
                    raise RuntimeError("disk full")

            shift = {"seconds": 0}

            def step_clock(_delay: float) -> None:
                shift["seconds"] += term + 1
                store.shift_clock(shift["seconds"])

            from shiftlease.logjson import JsonLogger
            from shiftlease.worker import Worker

            worker = Worker(
                store,
                Boom(),  # type: ignore[arg-type]
                "holder-a",
                store.config,
                random.Random(1),
                step_clock,
                JsonLogger(),
            )
            self.assertEqual(worker.run_until_idle(max_idle=2, spin_limit=50), "drained")
            counts = store.counts()
            self.assertEqual(counts["dead"], 1)
            self.assertEqual(counts["pending_intents"], 1)
            row = store.job_by_key("slip-1")
            assert row is not None
            self.assertIn("disk full", row["last_error"])
            store.close()

    def test_successful_job_is_not_dead_lettered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30, max_attempts=1)
            store.submit("slip-1", helpers.slip(), max_attempts=1)
            worker, _effect, _logger, _sleeper = helpers.make_worker(store, root / "out")
            self.assertEqual(worker.run_once(), "job")
            self.assertEqual(store.sweep_dead(), [])
            self.assertEqual(store.job_by_key("slip-1")["status"], "succeeded")
            store.close()
