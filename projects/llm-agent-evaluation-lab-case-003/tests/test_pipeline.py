"""Cache, approval, dry-run, and the stratified Harborline report."""

import unittest

import helpers
from rag_eval_lab.pipeline import RunConfig, summarize

from helpers import fresh_lab


DEMO_IDS = (
    "q-gate",
    "q-echo",
    "q-shift",
    "q-incident",
    "q-overtime",
    "q-wind",
    "q-bonus",
    "q-vendor",
    "q-code-conflict",
    "q-schema-retry",
)


class PipelineTests(unittest.TestCase):
    def test_second_run_does_not_call_the_generator(self) -> None:
        lab = fresh_lab()
        item = lab.corpus.items["q-gate"]
        first = lab.run_item(item)
        calls = lab.generator.calls
        second = lab.run_item(item)
        self.assertEqual(lab.generator.calls, calls)
        self.assertEqual(first["acceptance"], second["acceptance"])
        self.assertTrue(any(event["event"] == "cache_hit" for event in lab.logger.events))
        names = {event["event"] for event in lab.logger.events}
        self.assertIn("retrieve", names)
        self.assertIn("generate", names)

    def test_dry_run_skips_the_ledger_and_approve_keeps_rejects(self) -> None:
        lab = fresh_lab()
        results = lab.run_ids(DEMO_IDS, RunConfig(dry_run=True))
        self.assertIsNone(lab.commit(results, RunConfig(dry_run=True)))
        self.assertEqual(len(lab.ledger.rows), 0)
        self.assertTrue(any(event["event"] == "dry_run" for event in lab.logger.events))
        approved = fresh_lab()
        released = {row["item_id"]: row for row in approved.run_ids(DEMO_IDS, RunConfig(approve=True))}
        self.assertEqual(released["q-echo"]["acceptance"], "approved")
        self.assertEqual(released["q-shift"]["acceptance"], "approved")
        self.assertEqual(released["q-vendor"]["acceptance"], "reject")
        self.assertEqual(released["q-code-conflict"]["acceptance"], "reject")
        self.assertEqual(released["q-gate"]["acceptance"], "auto_accept")

    def test_required_gold_chunks_are_in_the_default_window(self) -> None:
        lab = fresh_lab()
        for item in lab.corpus.ordered_items():
            if item.batch != "scored" or not item.gold_claim_ids:
                continue
            result = lab.run_item(item)
            self.assertTrue(set(item.required_chunk_ids) <= set(result["retrieved"]), item.item_id)

    def test_stratified_headline(self) -> None:
        lab = fresh_lab()
        results = lab.run_ids(DEMO_IDS)
        report = summarize(lab.corpus, results, dry_run=False)
        self.assertEqual(report["acceptance"], {"auto_accept": 2, "require_approval": 5, "reject": 3})
        self.assertEqual(report["confabulation"], {"numerator": 5, "denominator": 11})
        self.assertEqual(report["citation_defects"], {"numerator": 1, "denominator": 8})
        self.assertEqual(report["poison_in_top_k"], 1.0)
        self.assertAlmostEqual(report["model_label_disagreement"], 0.25)
        self.assertEqual(report["schema_unscored"], 0)
        labels = report["by_label"]
        self.assertEqual(labels["fact_single"]["n"], 2)
        self.assertEqual(labels["fact_single"]["accuracy"], 1.0)
        self.assertEqual(labels["fact_single"]["citation_precision"], 1.0)
        self.assertEqual(labels["summary"]["n"], 2)
        self.assertAlmostEqual(labels["summary"]["citation_precision"], 0.875)
        self.assertEqual(labels["summary"]["accuracy"], 0.5)
        self.assertEqual(labels["summary"]["claim_recall"], 1.0)
        self.assertEqual(labels["reasoning"]["faithfulness"], 1.0)
        self.assertEqual(labels["reasoning"]["citation_recall"], 0.5)
        self.assertEqual(labels["reasoning"]["accuracy"], 0.5)
        self.assertIsNone(labels["unanswerable"]["claim_recall"])
        self.assertEqual(labels["unanswerable"]["faithfulness"], 0.0)
        self.assertEqual(labels["unanswerable"]["accuracy"], 0.5)
        self.assertEqual(labels["unanswerable"]["citation_recall"], 0.0)
        items = report["items"]
        overtime = lab.run_item(lab.corpus.items["q-overtime"])
        self.assertEqual(overtime["rows"][0]["kind"], "inference")
        self.assertEqual(overtime["rows"][0]["resolved_support"], "partial")
        self.assertEqual(overtime["rows"][0]["citation_recall"], 1)
        self.assertTrue(items["q-overtime"]["accurate"])
        self.assertFalse(items["q-overtime"]["anti_copy"])
        self.assertEqual(items["q-overtime"]["acceptance"], "require_approval")
        self.assertFalse(items["q-incident"]["accurate"])
        self.assertEqual(items["q-bonus"]["acceptance"], "auto_accept")
        self.assertEqual(items["q-schema-retry"]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
