import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from automation_job_lab.checkpoint import FileCheckpointStore, MemoryCheckpointStore
from automation_job_lab.errors import CheckpointError
from automation_job_lab.models import Checkpoint


class CheckpointTests(unittest.TestCase):
    def test_memory_roundtrip(self):
        store = MemoryCheckpointStore()
        item = Checkpoint(
            pipeline="hourly-ops",
            run_id="hourly-ops:w1",
            status="in_progress",
            window_start_ms=1,
            completed_jobs=["heartbeat-log"],
        )
        store.save(item)
        loaded = store.load("hourly-ops:w1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.completed_jobs, ["heartbeat-log"])
        self.assertIsNot(loaded, item)

    def test_file_atomic_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            item = Checkpoint(pipeline="hourly-ops", run_id="hourly-ops:w1", status="complete")
            store.save(item)
            path = store.path_for("hourly-ops:w1")
            self.assertTrue(path.exists())
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
            loaded = store.load("hourly-ops:w1")
            self.assertEqual(loaded.status, "complete")

    def test_corrupt_file_raises_checkpoint_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            path = store.path_for("hourly-ops:w1")
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CheckpointError) as ctx:
                store.load("hourly-ops:w1")
            self.assertIn("corrupt checkpoint", ctx.exception.message)

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(ValueError):
            Checkpoint.from_dict(
                {"pipeline": "hourly-ops", "run_id": "x", "status": "weird"}
            )

    def test_missing_run_id_is_rejected(self):
        with self.assertRaises(ValueError):
            Checkpoint.from_dict({"pipeline": "hourly-ops"})

    def test_load_missing_returns_none(self):
        self.assertIsNone(MemoryCheckpointStore().load("missing"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(FileCheckpointStore(tmp).load("missing"))

    def test_file_payload_is_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            store.save(Checkpoint(pipeline="hourly-ops", run_id="abc:def", status="in_progress"))
            raw = json.loads(Path(store.path_for("abc:def")).read_text(encoding="utf-8"))
            self.assertEqual(raw["run_id"], "abc:def")
            self.assertEqual(store.path_for("abc:def").name, "abc_def.json")
