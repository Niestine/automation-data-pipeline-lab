"""Prefix cuts of the shelf publish protocols."""

from __future__ import annotations

import json
import unittest

from helpers import ROOT

from prefixlab.errors import OracleFault
from prefixlab.publish import (
    crash_cuts,
    fault_if_corrupt,
    mount_shelf,
    rename_before_data_fsync,
    return_before_dir_fsync,
    safe_publish,
    scheduled_reorder,
)


def _bytes():
    doc = json.loads((ROOT / "examples" / "shelf_bytes.json").read_text(encoding="utf-8"))
    return doc["old"].encode("utf-8"), doc["new"].encode("utf-8"), int(doc["sector_size"])


class PublishTests(unittest.TestCase):
    def test_safe_ordered_profile_never_publishes_garbage(self):
        old, new, sector = _bytes()
        fs = mount_shelf("ordered_atomic", old, sector)
        safe_publish(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        self.assertEqual([row["outcome"] for row in cuts if row["outcome"] == "corrupt"], [])
        self.assertTrue(all(row["outcome"] in ("old", "new") for row in cuts))
        self.assertTrue(all(row["outcome"] == "new" for row in cuts if row["success"]))
        fault_if_corrupt(cuts)

    def test_rename_before_data_fsync_is_corrupt_at_cut_4(self):
        old, new, sector = _bytes()
        fs = mount_shelf("ordered_atomic", old, sector)
        rename_before_data_fsync(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        with self.assertRaises(OracleFault) as caught:
            fault_if_corrupt(cuts)
        self.assertEqual(caught.exception.payload["outcome"], "corrupt")
        self.assertEqual(caught.exception.payload["cut"], 4)
        self.assertEqual(caught.exception.payload["transactions"], [])

    def test_success_before_directory_fsync_loses_the_new_name(self):
        old, new, sector = _bytes()
        fs = mount_shelf("ordered_atomic", old, sector)
        return_before_dir_fsync(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        losses = [row for row in cuts if row["outcome"] == "durability_loss"]
        self.assertEqual([row["cut"] for row in losses], [5])

    def test_success_before_directory_fsync_raises_a_durability_payload(self):
        old, new, sector = _bytes()
        fs = mount_shelf("ordered_atomic", old, sector)
        return_before_dir_fsync(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        fault_if_corrupt(cuts)
        with self.assertRaises(OracleFault) as caught:
            fault_if_corrupt(cuts, outcomes=("durability_loss",))
        self.assertEqual(caught.exception.payload["outcome"], "durability_loss")
        self.assertEqual(caught.exception.payload["cut"], 5)

    def test_scheduled_reorder_of_the_safe_protocol_is_corrupt(self):
        old, new, sector = _bytes()
        fs = mount_shelf("ordered_atomic", old, sector)
        safe_publish(fs, 1, "shelf.dat", new)
        observed = scheduled_reorder(fs, 1, "shelf.dat", old, new)
        self.assertEqual(observed["outcome"], "corrupt")
        self.assertEqual(observed["cut"], "publish_rename_before_data_barrier")
        self.assertEqual(observed["persisted"], ["pin", "write", "rename", "fsync_dir"])
        self.assertEqual(observed["located_in"], [1])
        # The same issued operations never corrupt when only prefixes persist.
        self.assertNotIn("corrupt", [row["outcome"] for row in crash_cuts(fs, 1, "shelf.dat", old, new)])

    def test_scheduled_reorder_needs_a_data_barrier(self):
        old, new, sector = _bytes()
        fs = mount_shelf("ordered_atomic", old, sector)
        rename_before_data_fsync(fs, 1, "shelf.dat", new)
        with self.assertRaises(ValueError):
            scheduled_reorder(fs, 1, "shelf.dat", old, new)

    def test_relaxed_non_atomic_rename_is_an_assumption_failure(self):
        old, new, sector = _bytes()
        fs = mount_shelf("relaxed", old, sector)
        safe_publish(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        self.assertGreaterEqual(sum(row["outcome"] == "corrupt" for row in cuts), 1)
        self.assertIn(4, [row["cut"] for row in cuts if row["outcome"] == "corrupt"])

    def test_sector_atomic_write_does_not_tear(self):
        old = b"OLD-FILE"
        new = b"12345678"
        self.assertEqual(len(new), 8)
        fs = mount_shelf("ordered_atomic", old, 8)
        rename_before_data_fsync(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        self.assertEqual([row["outcome"] for row in cuts if row["outcome"] == "corrupt"], [])

    def test_unknown_profile_is_refused(self):
        with self.assertRaises(ValueError):
            mount_shelf("ext4-ordered", b"old", 8)


if __name__ == "__main__":
    unittest.main()
