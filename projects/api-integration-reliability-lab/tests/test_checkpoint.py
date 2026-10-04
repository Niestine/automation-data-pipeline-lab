import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import make_job

from api_reliability_lab.checkpoint import FileCheckpointStore, MemoryCheckpointStore
from api_reliability_lab.errors import CheckpointError, SimulatedCrash
from api_reliability_lab.ledger import Ledger
from api_reliability_lab.models import Checkpoint


class CheckpointTests(unittest.TestCase):
    def test_memory_round_trip_copies_state(self):
        store = MemoryCheckpointStore()
        original = Checkpoint(sync_id="lab-sync", cursor="abc", pages_done=2, orders_seen=20)
        store.save(original)
        original.cursor = "mutated"
        loaded = store.load("lab-sync")
        self.assertEqual(loaded.cursor, "abc")
        self.assertEqual(loaded.pages_done, 2)

    def test_file_store_is_atomic_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            store.save(Checkpoint(sync_id="lab-sync", phase="webhooks", pages_done=1))
            loaded = store.load("lab-sync")
            self.assertEqual(loaded.phase, "webhooks")
            self.assertTrue((Path(tmp) / "lab-sync.json").exists())
            self.assertFalse((Path(tmp) / "lab-sync.json.tmp").exists())

    def test_corrupt_checkpoint_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab-sync.json"
            path.write_text("{not json", encoding="utf-8")
            store = FileCheckpointStore(tmp)
            with self.assertRaises(CheckpointError):
                store.load("lab-sync")

    def test_invalid_phase_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab-sync.json"
            path.write_text(json.dumps({"sync_id": "lab-sync", "phase": "nope"}), encoding="utf-8")
            store = FileCheckpointStore(tmp)
            with self.assertRaises(CheckpointError):
                store.load("lab-sync")

    def test_ledger_rejects_tampered_rows_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            row = {"id": "ORD-1001", "status": "lost"}
            path.write_text(json.dumps({"orders": [row]}), encoding="utf-8")
            with self.assertRaises(CheckpointError):
                Ledger(path)

    def test_crash_before_checkpoint_replays_the_same_page_without_duplicating(self):
        job, service, *_ = make_job(crash_after_pages=1, crash_at="post_upsert")
        with self.assertRaises(SimulatedCrash):
            job.run(deliveries=[])
        self.assertEqual(len(job.ledger.orders), 10)
        checkpoint = job.store.load("lab-sync")
        self.assertEqual(checkpoint.pages_done, 0)
        self.assertIsNone(checkpoint.cursor)

        job.crash_after_pages = None
        report = job.run(deliveries=[])
        self.assertTrue(report.resumed)
        self.assertEqual(len(job.ledger.orders), 24)
        self.assertEqual(len(set(job.ledger.orders)), 24)
        self.assertGreaterEqual(report.orders_ignored_duplicate, 10)
        self.assertEqual(job.store.load("lab-sync").status, "complete")

    def test_crash_after_checkpoint_resumes_at_the_next_page(self):
        job, *_ = make_job(crash_after_pages=1, crash_at="post_checkpoint")
        with self.assertRaises(SimulatedCrash):
            job.run(deliveries=[])
        checkpoint = job.store.load("lab-sync")
        self.assertEqual(checkpoint.pages_done, 1)
        self.assertIsNotNone(checkpoint.cursor)
        job.crash_after_pages = None
        report = job.run(deliveries=[])
        self.assertEqual(len(job.ledger.orders), 24)
        self.assertEqual(report.pages, 2)

    def test_durable_ledger_survives_a_new_process_shaped_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            first = Ledger(path)
            job, *_ = make_job(crash_after_pages=1, crash_at="post_upsert", ledger=first)
            with self.assertRaises(SimulatedCrash):
                job.run(deliveries=[])
            reloaded = Ledger(path)
            self.assertEqual(len(reloaded.orders), 10)
            job2, *_ = make_job(ledger=reloaded, store=job.store)
            report = job2.run(deliveries=[])
            self.assertEqual(len(reloaded.orders), 24)
            self.assertEqual(report.checkpoint["status"], "complete")


if __name__ == "__main__":
    unittest.main()
