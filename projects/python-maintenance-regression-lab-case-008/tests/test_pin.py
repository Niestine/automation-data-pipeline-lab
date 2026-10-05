"""A pinned directory id keeps the rename in the directory that was opened."""

from __future__ import annotations

import json
import unittest

from helpers import ROOT

from prefixlab.publish import crash_cuts, mount_shelf, safe_publish, unpinned_publish


class PinTests(unittest.TestCase):
    def test_pinned_race_stays_in_the_original_directory(self):
        doc = json.loads((ROOT / "examples" / "shelf_bytes.json").read_text(encoding="utf-8"))
        old = doc["old"].encode("utf-8")
        new = doc["new"].encode("utf-8")
        fs = mount_shelf("ordered_atomic", old, int(doc["sector_size"]), race_parent=True)
        safe_publish(fs, 1, "shelf.dat", new)
        self.assertIn("move_parent", [op.kind for op in fs.ops])
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        self.assertTrue(all(row["outcome"] in ("old", "new") for row in cuts))
        self.assertTrue(all(row["located_in"] == [1] for row in cuts))
        self.assertEqual(cuts[-1]["outcome"], "new")

    def test_unpinned_race_is_unspecified(self):
        doc = json.loads((ROOT / "examples" / "shelf_bytes.json").read_text(encoding="utf-8"))
        old = doc["old"].encode("utf-8")
        new = doc["new"].encode("utf-8")
        fs = mount_shelf("ordered_atomic", old, int(doc["sector_size"]), race_parent=True)
        unpinned_publish(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        self.assertEqual(cuts[-1]["outcome"], "unspecified")
        self.assertTrue(any(row["outcome"] == "old" for row in cuts))
        # Resolved by path after the parent moved, the entry escaped into dir 2
        # while dir 1 still holds the old bytes.
        self.assertEqual(cuts[-1]["located_in"], [1, 2])

    def test_unpinned_publish_without_a_race_is_ordinary(self):
        doc = json.loads((ROOT / "examples" / "shelf_bytes.json").read_text(encoding="utf-8"))
        old = doc["old"].encode("utf-8")
        new = doc["new"].encode("utf-8")
        fs = mount_shelf("ordered_atomic", old, int(doc["sector_size"]))
        unpinned_publish(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        self.assertTrue(all(row["outcome"] in ("old", "new") for row in cuts))
        self.assertEqual(cuts[-1]["located_in"], [1])


if __name__ == "__main__":
    unittest.main()
