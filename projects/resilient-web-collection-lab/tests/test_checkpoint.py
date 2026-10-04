import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from web_collection_lab.checkpoint import FileCheckpointStore, MemoryCheckpointStore
from web_collection_lab.errors import CheckpointError
from web_collection_lab.models import Checkpoint


class CheckpointTests(unittest.TestCase):
    def test_memory_roundtrip(self):
        store = MemoryCheckpointStore()
        store.save(Checkpoint(job_id="lab-collect", phase="products", completed_paths=["/products/sku-1001"]))
        loaded = store.load("lab-collect")
        self.assertEqual(loaded.completed_paths, ["/products/sku-1001"])
        self.assertEqual(loaded.phase, "products")

    def test_file_roundtrip_and_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            store.save(Checkpoint(job_id="lab-collect", status="in_progress", products_seen=2))
            loaded = store.load("lab-collect")
            self.assertEqual(loaded.products_seen, 2)
            path = Path(tmp) / "lab-collect.checkpoint.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CheckpointError):
                store.load("lab-collect")

    def test_invalid_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            Checkpoint.from_dict({"job_id": "x", "phase": "nope", "status": "in_progress"})

    def test_missing_job_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            self.assertIsNone(store.load("missing"))

    def test_file_payload_is_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            store.save(Checkpoint(job_id="lab-collect"))
            payload = json.loads((Path(tmp) / "lab-collect.checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["job_id"], "lab-collect")


if __name__ == "__main__":
    unittest.main()
