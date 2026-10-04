"""Hand-checked claim ratios. Cosine overlap stays off the acceptance path."""

import inspect
import unittest

import helpers
from rag_eval_lab.diagnostics import answer_relevance, context_relevance, diagnose
from rag_eval_lab.scoring import decide_acceptance

from rag_eval_lab.scoring import resolve_claims
from rag_eval_lab.slices import hits_from_ids, ready_payload

from helpers import cite, claim, envelope, fresh_lab


class DiagnosticTests(unittest.TestCase):
    def test_planted_ratio_arithmetic(self) -> None:
        metrics = diagnose(
            ["G1", "G2"],
            ["G1", "G2", "F1", "N1", "H"],
            [{"A"}, set(), {"A"}, {"B"}, set()],
            ["A", "B"],
            {"A"},
            {"G1"},
        )
        self.assertAlmostEqual(metrics["precision"], 2 / 5)
        self.assertAlmostEqual(metrics["recall"], 1.0)
        self.assertAlmostEqual(metrics["f1"], 4 / 7)
        self.assertAlmostEqual(metrics["claim_recall"], 1 / 2)
        self.assertAlmostEqual(metrics["context_precision"], 1 / 2)
        self.assertAlmostEqual(metrics["faithfulness"], 3 / 5)
        self.assertAlmostEqual(metrics["relevant_noise"], 1 / 5)
        self.assertAlmostEqual(metrics["irrelevant_noise"], 1 / 5)
        self.assertAlmostEqual(metrics["hallucination"], 1 / 5)
        self.assertAlmostEqual(metrics["self_knowledge"], 1 / 5)
        self.assertAlmostEqual(metrics["context_utilization"], 1.0)

    def test_empty_retrieval_and_mixed_chunk(self) -> None:
        empty = diagnose(["G1"], ["G1"], [set()], [], set(), set())
        self.assertEqual(empty["claim_recall"], 0.0)
        self.assertEqual(empty["context_precision"], 0.0)
        self.assertEqual(empty["self_knowledge"], 1.0)
        mixed = diagnose(
            ["G1"],
            ["G1", "F1"],
            [{"M"}, {"M"}],
            ["M"],
            {"M"},
            {"G1"},
        )
        self.assertEqual(mixed["context_precision_den"], 1)
        self.assertEqual(mixed["context_precision"], 1.0)
        self.assertAlmostEqual(mixed["relevant_noise"], 0.5)

    def test_restatement_cosine_does_not_gate_acceptance(self) -> None:
        question = "What is the Harborline Depot north gate code?"
        self.assertAlmostEqual(answer_relevance(question, [question]), 1.0)
        self.assertEqual(answer_relevance(question, []), 0.0)
        metrics = diagnose(["G1"], ["F1"], [set()], ["A"], set(), set())
        self.assertEqual(metrics["f1"], 0.0)
        self.assertNotIn("relevance", inspect.signature(decide_acceptance).parameters)
        decision = decide_acceptance(
            scored=True,
            fail_closed=False,
            structured_abstain=False,
            accurate=False,
            rows=[{"resolved_support": "unsupported", "citation_recall": 0, "citation_precision": 0.0, "extra_defects": 0, "kind": "fact"}],
            copied=False,
            metrics=metrics,
        )
        self.assertEqual(decision, "reject")
        self.assertEqual(context_relevance(["kept sentence"], [True], True), 0.0)

    def test_logged_relevance_on_a_clean_answer(self) -> None:
        lab = fresh_lab()
        result = lab.run_item(lab.corpus.items["q-gate"])
        self.assertGreater(result["answer_relevance"], 0.0)
        self.assertEqual(result["acceptance"], "auto_accept")
        self.assertIn("context_relevance", result)
        item = lab.corpus.items["q-gate"]
        payload = envelope(
            "q-gate",
            [claim("G-GATE", "The Harborline Depot north gate code is 4419.", citations=[cite("ch-gate", "north gate code is 4419")])],
            surface="The code is 4419.",
        )
        ready = ready_payload(lab.corpus, item, payload)
        mixed = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-gate", "ch-rumor"]))
        self.assertAlmostEqual(mixed["context_relevance"], 0.5)
        self.assertEqual(mixed["acceptance"], "auto_accept")
        bonus = lab.run_item(lab.corpus.items["q-bonus"])
        self.assertEqual(bonus["context_relevance"], 0.0)


if __name__ == "__main__":
    unittest.main()
