"""The real heartbeat thread keeps a slow effect's lease alive.

These tests use the wall clock because the heartbeat is a timer thread.
A 3-second term and a 1-second heartbeat keep each run under 4 seconds.
"""

from __future__ import annotations

import random
import tempfile
import time
import unittest
from pathlib import Path

import helpers
from shiftlease.effect import CsvEffect
from shiftlease.logjson import JsonLogger
from shiftlease.worker import Worker

TERM = 3
EFFECT_SECONDS = 3.5


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_thread_renews_past_the_original_term(self) -> None:
        worker, store, logger = self._slow_run(heartbeat_seconds=1)
        row = store.job_by_key("slip-1")
        assert row is not None
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["fence"], 1)
        self.assertEqual(row["attempts"], 1)
        self.assertFalse(worker.jeopardy)
        renewals = [item for item in logger.records if item["event"] == "renewed"]
        self.assertGreaterEqual(len(renewals), 2)
        self.assertTrue(all(item["fence"] == 1 and item["job_id"] == row["id"] for item in renewals))

    def test_without_heartbeat_the_same_effect_cannot_complete(self) -> None:
        worker, store, logger = self._slow_run(heartbeat_seconds=0)
        row = store.job_by_key("slip-1")
        assert row is not None
        self.assertEqual(row["status"], "leased")
        self.assertTrue(worker.jeopardy)
        self.assertIn("stale_complete", [item["event"] for item in logger.records])
        self.assertEqual([item["kind"] for item in store.events(row["id"])], ["claim"])

    def _slow_run(self, heartbeat_seconds: int):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = helpers.open_store(root, lease_term_seconds=TERM, heartbeat_seconds=heartbeat_seconds)
        self.addCleanup(store.close)
        store.submit("slip-1", helpers.slip())
        inner = CsvEffect(root / "out")

        class Slow:
            def perform(self, **kwargs: object) -> dict:
                time.sleep(EFFECT_SECONDS)
                return inner.perform(**kwargs)

        logger = JsonLogger()
        worker = Worker(
            store,
            Slow(),  # type: ignore[arg-type]
            "holder-a",
            store.config,
            random.Random(1),
            lambda _delay: None,
            logger,
        )
        worker.start_heartbeat()
        try:
            self.assertEqual(worker.run_once(), "job")
        finally:
            worker.stop_heartbeat()
        return worker, store, logger
