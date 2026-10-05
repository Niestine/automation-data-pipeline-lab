import helpers  # noqa: F401
import json
import unittest

from incremental_crawl_lab.corpus import AD_EDIT, BASE, PARAGRAPH_EDIT, TIMESTAMP_EDIT
from incremental_crawl_lab.fingerprint import (
    calibrate_corpus,
    choose_k,
    hamming,
    simhash_from_hashes,
    simhash_text,
)


class FingerprintTest(unittest.TestCase):
    def test_sign_rule_and_zero_coordinate(self) -> None:
        cancelled = simhash_from_hashes([(0, 1), ((1 << 64) - 1, 1)])
        self.assertEqual(cancelled, 0)
        bit_zero = simhash_from_hashes([(1, 1)])
        self.assertEqual(bit_zero & 1, 1)
        self.assertEqual((bit_zero >> 1) & 1, 0)

    def test_identical_tokens_have_distance_zero(self) -> None:
        self.assertEqual(hamming(simhash_text(BASE), simhash_text(BASE)), 0)

    def test_calibration_separates_cosmetic_edits_and_is_not_three(self) -> None:
        found = calibrate_corpus()
        self.assertEqual(found["cosmetic"], {"timestamp": 6, "ad": 4})
        self.assertEqual(found["paragraph"], 24)
        self.assertEqual(found["simhash_k"], 23)
        self.assertNotEqual(found["simhash_k"], 3)
        self.assertLessEqual(hamming(simhash_text(BASE), simhash_text(TIMESTAMP_EDIT)), 23)
        self.assertLessEqual(hamming(simhash_text(BASE), simhash_text(AD_EDIT)), 23)
        self.assertGreater(hamming(simhash_text(BASE), simhash_text(PARAGRAPH_EDIT)), 23)
        saved = json.loads((helpers.EXAMPLES / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["simhash_k"], found["simhash_k"])
        self.assertTrue(saved["material_detection"])

    def test_overlapping_ranges_disable_the_cut(self) -> None:
        self.assertIsNone(choose_k([3], 3))
        self.assertEqual(choose_k([1], 3), 2)


if __name__ == "__main__":
    unittest.main()
