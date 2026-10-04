"""Confabulation rows are keyed by provider, prompt hash, and regression set."""

import json
import tempfile
import unittest
from pathlib import Path

import helpers
from rag_eval_lab.errors import LabError
from rag_eval_lab.ledger import ConfabulationLedger, prompt_hash
from rag_eval_lab.pipeline import RunConfig, confab_totals

from helpers import EXAMPLES, fresh_lab


class LedgerTests(unittest.TestCase):
    def test_same_key_is_idempotent_and_a_new_hash_appends(self) -> None:
        ledger = ConfabulationLedger()
        first = ledger.record("fake-script-v1", "a" * 16, "harborline-r1", 5, 11, 1, 8)
        second = ledger.record("fake-script-v1", "a" * 16, "harborline-r1", 5, 11, 1, 8)
        self.assertIs(first, second)
        self.assertEqual(len(ledger.rows), 1)
        self.assertAlmostEqual(first.rate, 5 / 11)
        ledger.record("fake-script-v1", "b" * 16, "harborline-r1", 5, 11, 1, 8)
        self.assertEqual(len(ledger.rows), 2)
        with self.assertRaises(LabError):
            ledger.record("fake-script-v1", "a" * 16, "harborline-r1", 4, 11, 1, 8)

    def test_round_trip_file(self) -> None:
        ledger = ConfabulationLedger()
        ledger.record("fake-script-v1", "c" * 16, "harborline-r1", 5, 11, 1, 8)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            ledger.save(path)
            loaded = ConfabulationLedger.load(path)
        self.assertEqual(loaded.rows[0].numerator, 5)
        self.assertEqual(loaded.rows[0].citation_defect_numerator, 1)
        self.assertEqual(len(prompt_hash("harborline")), 16)

    def test_regression_matches_the_golden_definition(self) -> None:
        golden = json.loads((EXAMPLES / "golden_confabulation.json").read_text(encoding="utf-8"))
        lab = fresh_lab()
        results = lab.run_ids(list(lab.corpus.regression_item_ids))
        numerator, denominator, defects, facts = confab_totals(results)
        self.assertEqual(numerator, golden["numerator"])
        self.assertEqual(denominator, golden["denominator"])
        self.assertEqual(defects, golden["citation_defect_numerator"])
        self.assertEqual(facts, golden["citation_defect_denominator"])
        self.assertEqual((numerator, denominator, defects, facts), (5, 11, 1, 8))
        row = lab.commit(results, RunConfig())
        self.assertEqual(row.prompt_hash, prompt_hash(lab.prompt))
        self.assertEqual(len(lab.ledger.rows), 1)
        lab.commit(results, RunConfig())
        self.assertEqual(len(lab.ledger.rows), 1)
        self.assertIn("unsupported claims", golden["definition"])


if __name__ == "__main__":
    unittest.main()
