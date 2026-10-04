import json
import unittest

import helpers
from llm_agent_lab.evaluation import GoldCase, evaluate, gold_from_dict, score_case
from llm_agent_lab.models import CONTRACT_VERSION, AgentOutput, RunResult, RunStatus


def _run(**overrides):
    payload = {
        "ticket_id": "T-1001",
        "status": RunStatus.COMPLETED,
        "contract_version": CONTRACT_VERSION,
        "input_hash": "abc",
        "run_id": "run",
        "attempts": 1,
        "output": AgentOutput(
            intent="status_lookup",
            confidence=0.9,
            entities={"record_id": "ORD-100"},
            proposed_action="lookup_record",
            action_args={"record_id": "ORD-100"},
            rationale="lookup",
            needs_human=False,
        ),
        "approval": "auto_allow",
        "error_code": None,
    }
    payload.update(overrides)
    return RunResult(**payload)


class EvaluationTests(unittest.TestCase):
    def test_perfect_case_scores_one(self):
        gold = GoldCase(
            ticket_id="T-1001",
            expected_status="completed",
            intent="status_lookup",
            proposed_action="lookup_record",
            expected_approval="auto_allow",
        )
        score = score_case(_run(), gold)
        self.assertEqual(score.ratio, 1.0)
        self.assertTrue(score.status_match)
        self.assertTrue(score.intent_match)

    def test_intent_mismatch_lowers_score(self):
        gold = GoldCase(
            ticket_id="T-1001",
            expected_status="completed",
            intent="summarize",
            proposed_action="lookup_record",
        )
        score = score_case(_run(), gold)
        self.assertFalse(score.intent_match)
        self.assertLess(score.ratio, 1.0)

    def test_blocked_case_does_not_require_intent(self):
        gold = GoldCase(
            ticket_id="T-1005",
            expected_status="blocked",
            expected_error_code="bulk_pii_export",
        )
        result = _run(
            ticket_id="T-1005",
            status=RunStatus.BLOCKED,
            output=None,
            approval=None,
            error_code="bulk_pii_export",
            attempts=0,
        )
        score = score_case(result, gold)
        self.assertIsNone(score.intent_match)
        self.assertTrue(score.status_match)
        self.assertEqual(score.ratio, 1.0)

    def test_aggregate_report_rates(self):
        gold = [
            GoldCase("T-1001", "completed", intent="status_lookup", proposed_action="lookup_record"),
            GoldCase("T-1002", "completed", intent="summarize", proposed_action="summarize"),
        ]
        results = [
            _run(),
            _run(
                ticket_id="T-1002",
                output=AgentOutput(
                    intent="summarize",
                    confidence=0.8,
                    entities={},
                    proposed_action="summarize",
                    action_args={"summary": "notes"},
                    rationale="summary",
                    needs_human=False,
                ),
                attempts=2,
            ),
        ]
        report = evaluate(results, gold)
        self.assertEqual(report["cases"], 2)
        self.assertEqual(report["status_accuracy"], 1.0)
        self.assertEqual(report["intent_accuracy"], 1.0)
        self.assertEqual(report["retry_rate"], 0.5)
        self.assertEqual(report["missing_results"], [])

    def test_missing_results_are_listed(self):
        report = evaluate([], [GoldCase("T-404", "completed")])
        self.assertEqual(report["missing_results"], ["T-404"])
        self.assertEqual(report["cases"], 0)

    def test_gold_from_dict_accepts_null_intent(self):
        gold = gold_from_dict(
            {
                "ticket_id": "T-1005",
                "intent": None,
                "proposed_action": None,
                "expected_status": "blocked",
                "expected_error_code": "bulk_pii_export",
            }
        )
        self.assertIsNone(gold.intent)
        self.assertEqual(gold.expected_status, "blocked")

    def test_contract_failure_counts_as_schema_invalid(self):
        gold = GoldCase("T-1001", "completed", intent="status_lookup")
        result = _run(status=RunStatus.FAILED, output=None, approval=None, error_code="parse_error", attempts=3)
        score = score_case(result, gold)
        self.assertIs(score.schema_valid, False)
        self.assertEqual(score.points, 0)
        report = evaluate([result], [gold])
        self.assertEqual(report["schema_validity_rate"], 0.0)

    def test_blocked_before_provider_is_schema_not_applicable(self):
        gold = GoldCase("T-1005", "blocked", expected_error_code="bulk_pii_export")
        result = _run(
            ticket_id="T-1005",
            status=RunStatus.BLOCKED,
            output=None,
            approval=None,
            error_code="bulk_pii_export",
            attempts=0,
        )
        score = score_case(result, gold)
        self.assertIsNone(score.schema_valid)
        self.assertIsNone(evaluate([result], [gold])["schema_validity_rate"])

    def test_example_gold_labels_cover_every_example_ticket(self):
        gold = json.loads((helpers.EXAMPLES / "gold_labels.json").read_text(encoding="utf-8"))
        tickets = json.loads((helpers.EXAMPLES / "tickets.json").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(row["ticket_id"] for row in gold),
            sorted(row["ticket_id"] for row in tickets),
        )


if __name__ == "__main__":
    unittest.main()
