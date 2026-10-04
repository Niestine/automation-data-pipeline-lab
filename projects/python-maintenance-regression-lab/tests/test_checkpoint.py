import tempfile
import unittest

import helpers  # noqa: F401

from maintenance_lab.checkpoint import FileCheckpointStore, MemoryCheckpointStore, checkpoint_key
from maintenance_lab.errors import CheckpointError
from maintenance_lab.models import Checkpoint


class CheckpointTests(unittest.TestCase):
    def _sample(self) -> Checkpoint:
        return Checkpoint(
            week_id="2026-W01",
            feed_name="supplier_v1.csv",
            feed_sha256="abc",
            last_row=3,
            status="in_progress",
            applied_skus=("SKU-1001",),
            rejected=1,
        )

    def test_memory_roundtrip(self):
        store = MemoryCheckpointStore()
        item = self._sample()
        store.save(item)
        loaded = store.load(checkpoint_key(item.week_id, item.feed_name))
        self.assertEqual(loaded.last_row, 3)
        self.assertEqual(loaded.applied_skus, ("SKU-1001",))

    def test_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            item = self._sample()
            store.save(item)
            key = checkpoint_key(item.week_id, item.feed_name)
            loaded = store.load(key)
            self.assertEqual(loaded.status, "in_progress")
            self.assertTrue(store.path_for(key).exists())

    def test_missing_is_none(self):
        self.assertIsNone(MemoryCheckpointStore().load("nope"))

    def test_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(tmp)
            path = store.path_for("bad")
            path.write_text("{nope", encoding="utf-8")
            with self.assertRaises(CheckpointError):
                store.load("bad")


if __name__ == "__main__":
    unittest.main()
