"""Submit contract: fingerprint, in-flight conflict, replay, retention, schedule slot."""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

import helpers
from shiftlease.errors import SubmitError, ValidationError
from shiftlease.payload import fingerprint


class SubmitTests(unittest.TestCase):
    def test_matrix_and_canonical_fingerprint(self) -> None:
        with self._tmp() as tmp:
            store = helpers.open_store(tmp)
            key = "6f1c0c3e-8a1d-4e0b-9c2a-111111111111"
            created = store.submit(key, helpers.slip())
            self.assertEqual(created.outcome, "created")
            self.assertEqual(created.http_class, 201)
            self.assertEqual(created.attempts, 0)
            reordered = {"rows": [{"bin": "A01", "qty": 2, "sku": "HAT01"}], "desk": "lane-a"}
            inflight = store.submit(key, reordered)
            self.assertEqual(inflight.outcome, "conflict")
            self.assertEqual(inflight.http_class, 409)
            self.assertEqual(inflight.job_id, created.job_id)
            self.assertEqual(inflight.attempts, 0)
            self.assertEqual(store.counts()["queued"], 1)
            with self.assertRaises(SubmitError) as caught:
                store.submit(key, helpers.slip(qty=9))
            self.assertEqual(caught.exception.http_class, 422)
            self.assertEqual(caught.exception.code, "payload_mismatch")
            self.assertEqual(store.counts()["queued"], 1)
            with self.assertRaises(SubmitError) as missing:
                store.submit("  ", helpers.slip())
            self.assertEqual(missing.exception.http_class, 400)
            self.assertEqual(missing.exception.code, "missing_idempotency_key")
            worker, effect, _logger, _sleeper = helpers.make_worker(store, Path(tmp) / "out")
            self.assertEqual(worker.run_once(), "job")
            self.assertEqual(effect.created, [key])
            replay = store.submit(key, helpers.slip())
            self.assertEqual(replay.outcome, "replay")
            self.assertEqual(replay.http_class, 200)
            self.assertEqual(replay.job_id, created.job_id)
            self.assertIsNotNone(replay.result_json)
            self.assertEqual(store.counts()["succeeded"], 1)
            stored = store.job(created.job_id)
            assert stored is not None
            self.assertEqual(stored["payload_fingerprint"], fingerprint(helpers.slip()))
            store.close()

    def test_validation_and_schedule_slot_is_one_row(self) -> None:
        with self._tmp() as tmp:
            store = helpers.open_store(tmp)
            with self.assertRaises(ValidationError):
                store.submit("key-a", {"desk": "lane-a", "rows": [{"sku": "HAT01", "qty": True, "bin": "A01"}]})
            with self.assertRaises(ValidationError):
                store.submit("key-b", {"desk": "lane-a", "rows": [{"sku": "HAT01", "qty": 1, "bin": "A01"}], "note": "x"})
            slot = json.loads((helpers.EXAMPLES / "schedule_slot.json").read_text(encoding="utf-8"))
            first = store.enqueue_slot(slot["schedule_id"], slot["slot_start"], slot["payload"])
            second = store.enqueue_slot(slot["schedule_id"], slot["slot_start"], slot["payload"])
            self.assertEqual(second.outcome, "conflict")
            self.assertEqual(first.job_id, second.job_id)
            self.assertEqual(store.job(first.job_id)["idempotency_key"], "sched:weekly-lane:2026-10-05T00:00:00Z")
            worker, _effect, _logger, _sleeper = helpers.make_worker(store, Path(tmp) / "out")
            self.assertEqual(worker.run_once(), "job")
            third = store.enqueue_slot(slot["schedule_id"], slot["slot_start"], slot["payload"])
            self.assertEqual(third.outcome, "replay")
            self.assertEqual(store.counts()["succeeded"], 1)
            store.close()

    def test_purge_makes_a_key_reusable_only_after_the_record_is_gone(self) -> None:
        with self._tmp() as tmp:
            store = helpers.open_store(tmp, retention_seconds=10)
            created = store.submit("keep-me", helpers.slip())
            worker, _effect, _logger, _sleeper = helpers.make_worker(store, Path(tmp) / "out")
            self.assertEqual(worker.run_once(), "job")
            self.assertEqual(store.purge_expired(), 0)
            self.assertIsNotNone(store.job_by_key("keep-me"))
            store.shift_clock(11)
            self.assertEqual(store.purge_expired(), 1)
            self.assertIsNone(store.job_by_key("keep-me"))
            again = store.submit("keep-me", helpers.slip())
            self.assertEqual(again.outcome, "created")
            self.assertNotEqual(again.job_id, created.job_id)
            store.close()

    def test_future_run_at_waits_for_the_database_clock(self) -> None:
        with self._tmp() as tmp:
            store = helpers.open_store(tmp)
            with store._lock:
                later = store._scalar("datetime('now', '+7200 seconds')")
            store.submit("later", helpers.slip(), run_at=later)
            missed = store.claim("holder-a", random.Random(1))
            self.assertEqual(missed.kind, "empty")
            store.shift_clock(10800)
            taken = store.claim("holder-a", random.Random(1))
            self.assertEqual(taken.kind, "job")
            store.close()

    def _tmp(self):
        import tempfile

        return tempfile.TemporaryDirectory()
