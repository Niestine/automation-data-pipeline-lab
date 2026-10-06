import unittest
import unicodedata

import helpers  # noqa: F401

from edition_gate.link import standardize
from edition_gate.normalize import length_report, match_key, nfc, nfkc, unidata_version


class NormalizeTests(unittest.TestCase):
    def test_stable_compatibility_folds(self):
        self.assertEqual(nfkc("\ufb01"), "fi")
        self.assertEqual(len("\ufb01"), 1)
        self.assertEqual(len(nfkc("\ufb01")), 2)
        self.assertEqual(nfkc("\u3300"), "アパート")
        self.assertEqual(len("\u3300"), 1)
        self.assertEqual(len(nfkc("\u3300")), 4)
        self.assertEqual(nfc("\uff21"), "\uff21")
        self.assertEqual(nfkc("\uff21"), "A")
        self.assertEqual(nfkc("\uff71"), "\u30a2")

    def test_stored_form_stays_nfc_and_match_key_does_not_casefold(self):
        report = length_report("\ufb01tting")
        self.assertEqual(report["nfc"], "\ufb01tting")
        self.assertEqual(report["nfkc"], "fitting")
        self.assertTrue(report["compatibility_fold"])
        self.assertNotEqual(report["raw_length"], report["nfkc_length"])
        self.assertNotEqual(match_key("HEX"), match_key("hex"))
        self.assertEqual(standardize("HEX Bolt"), "hex bolt")
        self.assertEqual(unidata_version(), unicodedata.unidata_version)


if __name__ == "__main__":
    unittest.main()
