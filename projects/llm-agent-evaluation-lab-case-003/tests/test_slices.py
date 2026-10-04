"""Construction quotas, noise curve, integration, and memory boundaries."""

import json
import unittest

import helpers
from rag_eval_lab.corpus import is_fact_single_heavy, load_corpus_dict, quota_counts, reject_if_collapsed, select_scored
from rag_eval_lab.errors import LabInputError
from rag_eval_lab.hybrid import best_text_weights
from rag_eval_lab.models import CANONICAL_REFUSAL
from rag_eval_lab.scoring import require_stratified, resolve_claims
from rag_eval_lab.slices import hits_from_ids, ready_payload, run_noise_curve

from helpers import EXAMPLES, cite, claim, envelope, fresh_lab


class SliceTests(unittest.TestCase):
    def test_quotas_and_one_shot_control(self) -> None:
        lab = fresh_lab()
        items = lab.corpus.ordered_items()
        self.assertEqual(quota_counts(items), lab.corpus.quotas)
        self.assertEqual(lab.corpus.quotas, {"fact_single": 2, "summary": 2, "reasoning": 2, "unanswerable": 2})
        scored = select_scored(items)
        self.assertEqual(len(scored), 8)
        one_shot = [item for item in items if item.batch == "one_shot"]
        self.assertEqual(len(one_shot), 4)
        self.assertTrue(is_fact_single_heavy(one_shot))
        self.assertTrue(all(item.statement_first is None for item in one_shot))
        self.assertFalse(any(item.item_id in {row.item_id for row in scored} for item in one_shot))
        with self.assertRaises(LabInputError):
            reject_if_collapsed(one_shot, lab.corpus.quotas)

    def test_statement_first_and_quota_checks_reject_bad_corpora(self) -> None:
        raw = json.loads((EXAMPLES / "corpus.json").read_text(encoding="utf-8"))
        load_corpus_dict(json.loads(json.dumps(raw)))

        def mutated(item_id: str, **fields: object) -> dict:
            copy = json.loads(json.dumps(raw))
            entry = next(item for item in copy["items"] if item["item_id"] == item_id)
            for key, value in fields.items():
                if key in (entry.get("statement_first") or {}):
                    entry["statement_first"][key] = value
                else:
                    entry[key] = value
            return copy

        quota = json.loads(json.dumps(raw))
        quota["quotas"]["summary"] = 3
        cases = [
            ("fact_single samples one corpus span", mutated("q-gate", sampled_statements=["The gate code is posted."])),
            (
                "reasoning answer must not be a stored span",
                mutated(
                    "q-overtime",
                    sampled_statements=["Overtime is paid at one and a half times the base rate."],
                    conclusion_statements=["Overtime is paid at one and a half times the base rate.", "b", "c"],
                ),
            ),
            ("summary_statements is short", mutated("q-shift", summary_statements=["only one"])),
            ("statement_first is required", mutated("q-incident", statement_first=None)),
            ("built as topical negatives", mutated("q-bonus", statement_first={"theme": "pay"})),
            # Relabeling a reasoning item as a fact fails construction, not just the quota count.
            ("fact_single samples one corpus span", mutated("q-wind", construction_label="fact_single")),
            ("quota mismatch", quota),
        ]
        for message, payload in cases:
            with self.assertRaises(LabInputError, msg=message) as caught:
                load_corpus_dict(payload)
            self.assertIn(message, str(caught.exception))

    def test_stratified_report_is_required(self) -> None:
        with self.assertRaises(LabInputError):
            require_stratified({"by_label": {"fact_single": {}, "summary": {}, "reasoning": {}}})

    def test_noise_curve_drops_accuracy_at_the_top_ratio(self) -> None:
        lab = fresh_lab()
        script = json.loads((EXAMPLES / "noise_script.json").read_text(encoding="utf-8"))
        curve = run_noise_curve(lab.corpus, lab.corpus.items["q-lamp"], script["ratios"])
        self.assertEqual([point["ratio"] for point in curve["points"]], [0.0, 0.2, 0.4, 0.6, 0.8])
        self.assertEqual([point["accuracy"] for point in curve["points"]], [1.0, 1.0, 1.0, 1.0, 0.0])
        self.assertTrue(all(point["claim_recall"] == 1.0 for point in curve["points"]))
        self.assertTrue(curve["outside_memory"])
        full = curve["points"][0]["chunk_ids"]
        self.assertEqual(len(full), 5)
        self.assertTrue(all(chunk_id.startswith("ch-lamp-p") for chunk_id in full))
        noisy = curve["points"][-1]["chunk_ids"]
        self.assertEqual(noisy[0], "ch-lamp-p1")
        self.assertEqual(len(noisy), 5)
        self.assertIn("ch-lamp-n1", noisy)

    def test_missing_partner_chunk_drops_the_second_fact(self) -> None:
        lab = fresh_lab()
        item = lab.corpus.items["q-shift"]
        payload = helpers.load_script()[2]["q-shift"][0]
        ready = ready_payload(lab.corpus, item, payload)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-tally"]))
        self.assertNotIn("G-FUEL", result["gold_retrieved"])
        self.assertIn("G-TALLY", result["gold_retrieved"])
        self.assertAlmostEqual(result["metrics"]["claim_recall"], 0.5)

    def test_mis_bound_citation_has_recall_zero(self) -> None:
        lab = fresh_lab()
        payload = envelope(
            "q-shift",
            [
                claim(
                    "G-FUEL",
                    "Shift open requires a signed fuel sheet.",
                    citations=[cite("ch-tally", "printed dock tally")],
                )
            ],
        )
        item = lab.corpus.items["q-shift"]
        ready = ready_payload(lab.corpus, item, payload)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-tally", "ch-fuel"]))
        self.assertEqual(result["rows"][0]["citation_recall"], 0)
        self.assertEqual(result["rows"][0]["resolved_support"], "unsupported")

    def test_overtime_needs_both_premise_chunks(self) -> None:
        lab = fresh_lab()
        item = lab.corpus.items["q-overtime"]
        payload = helpers.load_script()[2]["q-overtime"][0]
        ready = ready_payload(lab.corpus, item, payload)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-ot-hours"]))
        self.assertEqual(result["gold_retrieved"], [])
        self.assertEqual(result["metrics"]["claim_recall"], 0.0)

    def test_memory_boundary_and_refusal_string(self) -> None:
        lab = fresh_lab()
        memory = lab.corpus.provider_memory_claim_ids
        self.assertEqual(memory, frozenset({"G-RADIO"}))
        scored = select_scored(lab.corpus.ordered_items())
        scored_gold = {claim_id for item in scored for claim_id in item.gold_claim_ids}
        self.assertTrue(scored_gold.isdisjoint(memory))
        radio = lab.corpus.items["q-radio-cf"]
        self.assertFalse(radio.outside_memory)
        self.assertIn("G-RADIO", memory)
        for item_id in ("q-lamp", "q-bonus", "q-vendor", "q-shift"):
            item = lab.corpus.items[item_id]
            self.assertTrue(item.outside_memory)
            self.assertTrue(set(item.gold_claim_ids).isdisjoint(memory))
        bonus = lab.run_item(lab.corpus.items["q-bonus"])
        self.assertTrue(bonus["structured_abstain"])
        self.assertTrue(bonus["surface_refusal_match"])
        self.assertEqual(bonus["acceptance"], "auto_accept")
        self.assertTrue(bonus["accurate"])
        payload = helpers.load_script()[2]["q-bonus"][0]
        payload = json.loads(json.dumps(payload))
        payload["surface_text"] = "Insufficient evidence in the handbook."
        self.assertNotEqual(payload["surface_text"], CANONICAL_REFUSAL)
        item = lab.corpus.items["q-bonus"]
        ready = ready_payload(lab.corpus, item, payload)
        rewritten = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-pay-1"]))
        self.assertTrue(rewritten["accurate"])
        self.assertFalse(rewritten["surface_refusal_match"])
        self.assertEqual(rewritten["acceptance"], "auto_accept")

    def test_unplanted_fallback_cannot_raise_gold_recall(self) -> None:
        lab = fresh_lab()
        yes = {
            "text": "The depot was founded in 1888.",
            "explanation": "The gate chunk names the depot.",
            "verdict": "yes",
            "chunk_id": "ch-gate",
        }
        no = {
            "text": "The depot was founded in 1888.",
            "explanation": "No sentence states a founding year.",
            "verdict": "no",
            "chunk_id": None,
        }
        payload = envelope(
            "q-gate",
            [
                claim(
                    None,
                    "The depot was founded in 1888.",
                    support="unsupported",
                    fallback={"statements": [yes]},
                )
            ],
        )
        item = lab.corpus.items["q-gate"]
        ready = ready_payload(lab.corpus, item, payload)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-gate"]))
        self.assertEqual(result["metrics"]["recall"], 0.0)
        self.assertEqual(result["metrics"]["precision"], 0.0)
        self.assertEqual(result["metrics"]["claim_recall"], 1.0)
        self.assertEqual(result["rows"][0]["resolved_support"], "supported")
        mixed = envelope(
            "q-gate",
            [
                claim(
                    None,
                    "The depot was founded in 1888.",
                    support="unsupported",
                    fallback={"statements": [yes, no]},
                )
            ],
        )
        ready = ready_payload(lab.corpus, item, mixed)
        result = resolve_claims(lab.corpus, item, ready, hits_from_ids(lab.corpus, ["ch-gate"]))
        self.assertEqual(result["rows"][0]["resolved_support"], "unsupported")

    def test_hybrid_optima_differ_by_label(self) -> None:
        fixture = json.loads((EXAMPLES / "hybrid_fixture.json").read_text(encoding="utf-8"))
        report = best_text_weights(fixture["examples"], fixture["weights"], fixture["floor"])
        self.assertEqual(report["pooled_best_text_weight"], 0.0)
        self.assertEqual(report["reasoning_best_text_weight"], 1.0)
        self.assertTrue(report["optima_differ"])
        self.assertEqual(report["by_label"]["fact_single"]["0.50"], 0.0)
        self.assertEqual(report["by_label"]["summary"]["0.50"], 0.0)
        self.assertEqual(report["by_label"]["reasoning"]["0.50"], 0.0)
        self.assertAlmostEqual(report["pooled"]["0.00"], 4 / 6)


if __name__ == "__main__":
    unittest.main()
