import statistics
import unittest

import helpers  # noqa: F401

from slip_lab.budget import apply_cap


class CapTests(unittest.TestCase):
    def test_candidate_cap(self):
        blobs = [b"AAA~", b"BB~", b"C~", b"DDDD~", b"E~"]

        def interesting_for(_parent):
            def interesting(blob: bytes) -> bool:
                return b"~" in blob

            return interesting

        result = apply_cap(blobs, 2, interesting_for)
        self.assertEqual(result["committed"], [b"~", b"~"])
        self.assertEqual(result["overflow"], 3)
        self.assertAlmostEqual(result["median_shrink_ratio"], statistics.median([1 / 4, 1 / 3]))


if __name__ == "__main__":
    unittest.main()
