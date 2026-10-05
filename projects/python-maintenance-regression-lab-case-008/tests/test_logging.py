"""Failure payloads and the human-readable log line share a code."""

from __future__ import annotations

import json
import unittest

from helpers import ROOT, capture_logger

from prefixlab.errors import OracleFault
from prefixlab.publish import crash_cuts, fault_if_corrupt, mount_shelf, rename_before_data_fsync


class LoggingTests(unittest.TestCase):
    def test_corrupt_cut_log_line_matches_the_payload(self):
        doc = json.loads((ROOT / "examples" / "shelf_bytes.json").read_text(encoding="utf-8"))
        old = doc["old"].encode("utf-8")
        new = doc["new"].encode("utf-8")
        fs = mount_shelf("ordered_atomic", old, int(doc["sector_size"]))
        rename_before_data_fsync(fs, 1, "shelf.dat", new)
        cuts = crash_cuts(fs, 1, "shelf.dat", old, new)
        log, handler, lines = capture_logger()
        try:
            with self.assertRaises(OracleFault) as caught:
                fault_if_corrupt(cuts)
        finally:
            log.removeHandler(handler)
        payload = caught.exception.payload
        self.assertEqual(payload["outcome"], "corrupt")
        self.assertEqual(payload["cut"], 4)
        text = "\n".join(lines)
        self.assertIn("outcome=corrupt", text)
        self.assertIn("cut=4", text)


if __name__ == "__main__":
    unittest.main()
