"""Atomic citation recall, precision, anti-copy, and confabulation buckets."""

import unittest

import helpers
from rag_eval_lab.citations import citation_defect, score_citations
from rag_eval_lab.scoring import resolve_claims
from rag_eval_lab.slices import hits_from_ids, ready_payload

from helpers import cite, claim, envelope, fresh_lab


class CitationTests(unittest.TestCase):
    def test_recall_zero_redundant_and_fourth_citation(self) -> None:
        empty = score_citations([], lambda spans: True)
        self.assertEqual(empty["recall"], 0)
        self.assertEqual(empty["precision"], 0.0)
        missed = score_citations([{"id": "a"}, {"id": "b"}], lambda spans: False)
        self.assertEqual(missed["recall"], 0)
        self.assertEqual(missed["precision"], 0.0)
        self.assertTrue(all(row["precision"] == 0 for row in missed["citations"]))
        lone = score_citations([{"id": "a"}], lambda spans: True)
        self.assertEqual(lone["recall"], 1)
        self.assertEqual(lone["precision"], 1.0)
        redundant = score_citations([{"id": "a"}, {"id": "b"}], lambda spans: True)
        self.assertEqual(redundant["precision"], 1.0)
        self.assertTrue(all(row["irrelevant"] is False and row["precision"] == 1 for row in redundant["citations"]))
        fourth = score_citations([{"id": str(i)} for i in range(4)], lambda spans: True)
        self.assertEqual(fourth["recall"], 1)
        self.assertEqual(fourth["extra_defects"], 1)
        self.assertEqual(fourth["citations"][3]["precision"], 0)
        self.assertAlmostEqual(fourth["precision"], 0.75)

    def test_decorative_span_drops_precision_only(self) -> None:
        lab = fresh_lab()
        result = lab.run_item(lab.corpus.items["q-shift"])
        fuel = next(row for row in result["rows"] if row["claim_id"] == "G-FUEL")
        self.assertEqual(fuel["citation_recall"], 1)
        self.assertAlmostEqual(fuel["citation_precision"], 0.5)
        self.assertEqual(fuel["citation_rows"][0]["precision"], 1)
        self.assertTrue(fuel["citation_rows"][1]["irrelevant"])
        self.assertEqual(fuel["citation_rows"][1]["precision"], 0)
        tally = next(row for row in result["rows"] if row["claim_id"] == "G-TALLY")
        self.assertEqual(tally["citation_precision"], 1.0)
        self.assertEqual(result["acceptance"], "require_approval")
        self.assertTrue(result["accurate"])

    def test_echo_passes_citation_and_fails_anti_copy(self) -> None:
        lab = fresh_lab()
        echo = lab.run_item(lab.corpus.items["q-echo"])
        gate = lab.run_item(lab.corpus.items["q-gate"])
        self.assertEqual(echo["rows"][0]["citation_recall"], 1)
        self.assertTrue(echo["anti_copy"])
        self.assertEqual(echo["acceptance"], "require_approval")
        self.assertTrue(echo["accurate"])
        self.assertFalse(gate["anti_copy"])
        self.assertEqual(gate["acceptance"], "auto_accept")

    def test_internal_contradiction_counts_a_claim_twice(self) -> None:
        lab = fresh_lab()
        result = lab.run_item(lab.corpus.items["q-code-conflict"])
        self.assertTrue(all(row["citation_recall"] == 1 for row in result["rows"]))
        self.assertEqual(result["confab"]["contradicted"], 1)
        self.assertEqual(result["confab"]["internal"], 2)
        self.assertEqual(result["confab"]["numerator"], 3)
        self.assertEqual(result["confab"]["denominator"], 2)
        self.assertTrue(result["fail_closed"])
        self.assertEqual(result["fail_reason"], "gold_contradiction")
        self.assertEqual(result["acceptance"], "reject")
        rumor = lab.corpus.chunks["ch-rumor"]
        self.assertEqual(rumor.tags, ())

    def test_wrong_span_can_stay_context_faithful(self) -> None:
        lab = fresh_lab()
        payload = envelope(
            "q-gate",
            [
                claim(
                    "G-GATE",
                    "The Harborline Depot north gate code is 4419.",
                    citations=[cite("ch-gate", "The Harborline")],
                )
            ],
            surface="The code is 4419.",
        )
        item = lab.corpus.items["q-gate"]
        ready = ready_payload(lab.corpus, item, payload)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-gate"]))
        self.assertEqual(result["rows"][0]["citation_recall"], 0)
        self.assertEqual(result["rows"][0]["resolved_support"], "unsupported")
        self.assertEqual(result["metrics"]["faithfulness"], 1.0)
        self.assertEqual(result["metrics"]["self_knowledge"], 0.0)
        self.assertTrue(citation_defect("fact", 0, "supported"))
        self.assertFalse(citation_defect("fact", 0, "abstain"))
        self.assertFalse(citation_defect("inference", 0, "unsupported"))

    def test_empty_supported_citation_becomes_abstain(self) -> None:
        lab = fresh_lab()
        payload = envelope(
            "q-gate",
            [claim("G-GATE", "The Harborline Depot north gate code is 4419.", support="supported")],
        )
        item = lab.corpus.items["q-gate"]
        ready = ready_payload(lab.corpus, item, payload)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-gate"]))
        self.assertEqual(result["rows"][0]["resolved_support"], "abstain")
        self.assertEqual(result["claim_schema_defects"], 1)
        self.assertEqual(result["metrics"]["response_units"], 0)
        self.assertIsNone(result["metrics"]["faithfulness"])
        self.assertEqual(result["citation_defects"], 0)
        self.assertEqual(result["confab"]["denominator"], 1)
        self.assertEqual(result["confab"]["numerator"], 0)

    def test_wind_citation_miss_keeps_context_faithfulness(self) -> None:
        lab = fresh_lab()
        result = lab.run_item(lab.corpus.items["q-wind"])
        self.assertEqual(set(result["retrieved"]) & {"ch-wind-limit", "ch-wind-load"}, {"ch-wind-limit", "ch-wind-load"})
        row = result["rows"][0]
        self.assertEqual(row["model_support"], "supported")
        self.assertEqual(row["resolved_support"], "unsupported")
        self.assertEqual(row["citation_recall"], 0)
        self.assertEqual(result["metrics"]["faithfulness"], 1.0)
        self.assertEqual(result["acceptance"], "reject")
        self.assertFalse(result["accurate"])

    def test_fourth_citation_is_not_a_schema_failure(self) -> None:
        lab = fresh_lab()
        payload = envelope(
            "q-gate",
            [
                claim(
                    "G-GATE",
                    "The Harborline Depot north gate code is 4419.",
                    citations=[cite("ch-gate", "north gate code is 4419")] * 4,
                )
            ],
            surface="The code is 4419.",
        )
        item = lab.corpus.items["q-gate"]
        ready = ready_payload(lab.corpus, item, payload)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-gate"]))
        self.assertEqual(result["rows"][0]["extra_defects"], 1)
        self.assertEqual(result["rows"][0]["citation_recall"], 1)
        self.assertAlmostEqual(result["rows"][0]["citation_precision"], 0.75)
        self.assertEqual(result["acceptance"], "require_approval")
        self.assertTrue(result["scored"])


if __name__ == "__main__":
    unittest.main()
