"""Crashes around the external CSV still leave one applied intent and one file."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers
from shiftlease.effect import CsvEffect
from shiftlease.errors import CrashInjected
from shiftlease.worker import FaultPlan


class RecoveryTests(unittest.TestCase):
    def test_crash_after_intent_retries_the_effect_once(self) -> None:
        self._recover(FaultPlan(crash_after_intent=True), created=1)

    def test_crash_after_effect_replays_the_same_bytes(self) -> None:
        self._recover(FaultPlan(crash_after_effect=True), created=1, calls_after_resume=2)

    def test_crash_after_applied_mark_does_not_repeat_the_effect(self) -> None:
        self._recover(FaultPlan(crash_after_applied=True), created=1, calls_after_resume=1)

    def test_effect_retries_are_capped_separately_from_the_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30, effect_retry_cap=1, jitter_cap_seconds=0.0)
            store.submit("slip-1", helpers.slip(), max_attempts=3)

            class Boom:
                def __init__(self) -> None:
                    self.calls = 0

                def perform(self, **_kwargs: object) -> dict:
                    self.calls += 1
                    raise RuntimeError("disk busy")

            from shiftlease.logjson import JsonLogger
            from shiftlease.worker import Worker
            import random

            effect = Boom()
            sleeper = helpers.RecordingSleeper()
            worker = Worker(
                store,
                effect,  # type: ignore[arg-type]
                "holder-a",
                store.config,
                random.Random(1),
                sleeper,
                JsonLogger(),
            )
            self.assertEqual(worker.run_once(), "job")
            self.assertEqual(effect.calls, 2)
            self.assertEqual(len(sleeper.delays), 1)
            row = store.job_by_key("slip-1")
            assert row is not None
            self.assertEqual(row["status"], "leased")
            self.assertEqual(row["attempts"], 1)
            self.assertIn("disk busy", row["last_error"])
            # The abandoned lease is not renewed; it is left to expire.
            self.assertIsNone(worker.current)
            self.assertFalse(worker.renew_current())
            self.assertEqual(store.job_by_key("slip-1")["lease_until"], row["lease_until"])
            store.close()

    def test_effect_failure_does_not_disable_heartbeats_for_the_next_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30, effect_retry_cap=0)
            store.submit("slip-1", helpers.slip())
            store.submit("slip-2", helpers.slip(sku="COAT7", qty=1, bin_code="B12"))
            inner = CsvEffect(root / "out")
            holder: dict = {}

            class FailFirst:
                def __init__(self) -> None:
                    self.renewed: list[bool] = []

                def perform(self, *, idempotency_key: str, payload: dict, fence: int) -> dict:
                    if idempotency_key == "slip-1":
                        raise RuntimeError("disk busy")
                    self.renewed.append(holder["worker"].renew_current())
                    return inner.perform(idempotency_key=idempotency_key, payload=payload, fence=fence)

            from shiftlease.logjson import JsonLogger
            from shiftlease.worker import Worker
            import random

            effect = FailFirst()
            worker = Worker(
                store,
                effect,  # type: ignore[arg-type]
                "holder-a",
                store.config,
                random.Random(1),
                lambda _delay: None,
                JsonLogger(),
            )
            holder["worker"] = worker
            self.assertEqual(worker.run_once(), "job")
            self.assertEqual(store.job_by_key("slip-1")["status"], "leased")
            self.assertEqual(worker.run_once(), "job")
            self.assertEqual(effect.renewed, [True])
            self.assertFalse(worker.jeopardy)
            self.assertEqual(store.job_by_key("slip-2")["status"], "succeeded")
            store.close()

    def _recover(self, faults: FaultPlan, created: int, calls_after_resume: int | None = None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            worker, effect, _logger, _sleeper = helpers.make_worker(store, root / "out", faults=faults)
            with self.assertRaises(CrashInjected):
                worker.run_once()
            self.assertEqual(store.job_by_key("slip-1")["status"], "leased")
            faults.crash_after_intent = False
            faults.crash_after_effect = False
            faults.crash_after_applied = False
            worker.jeopardy = False
            worker.resume_held()
            row = store.job_by_key("slip-1")
            assert row is not None
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(row["fence"], 1)
            intent = store.intent("slip-1")
            assert intent is not None
            self.assertEqual(intent["state"], "applied")
            self.assertEqual(len(effect.created), created)
            if calls_after_resume is not None:
                self.assertEqual(len(effect.calls), calls_after_resume)
            csv_files = list((root / "out").glob("*.csv"))
            self.assertEqual(len(csv_files), 1)
            self.assertIn(b"slip-1", csv_files[0].read_bytes())
            store.close()
