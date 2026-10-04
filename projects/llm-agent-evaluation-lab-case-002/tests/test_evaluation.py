import unittest

from brief_router_lab.contracts import parse_tool_plan
from brief_router_lab.evaluation import GoldCase, evaluate, gold_from_dict, score_case
from brief_router_lab.models import CONTRACT_VERSION, RunResult, RunStatus
import helpers


def _run(**overrides):
    plan = parse_tool_plan(helpers.valid_plan_json())
    payload = {
        "packet_id": "P-2001",
        "status": RunStatus.COMPLETED,
        "contract_version": CONTRACT_VERSION,
        "input_hash": "abc",
        "run_id": "run",
        "attempts": 1,
        "plan": plan,
        "approval": "auto_allow",
        "error_code": None,
        "tokens_planned": 20,
        "tokens_used": 20,
        "token_budget": 200,
        "steps_planned": 1,
    }
    payload.update(overrides)
    return RunResult(**payload)


class EvaluationTests(unittest.TestCase):
    def test_perfect_routing_scores_one(self):
        gold = GoldCase(
            packet_id="P-2001",
            expected_status="completed",
            goal_kind="lookup",
            expected_tools=["catalog.search"],
        )
        score = score_case(_run(), gold)
        self.assertEqual(score.ratio, 1.0)
        self.assertTrue(score.sequence_match)
        self.assertEqual(score.tool_precision, 1.0)
        self.assertEqual(score.tool_recall, 1.0)

    def test_wrong_tool_lowers_precision_and_sequence(self):
        plan = parse_tool_plan(
            helpers.valid_plan_json(
                budget_tokens=10,
                steps=[{"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}}],
            )
        )
        gold = GoldCase(
            packet_id="P-2001",
            expected_status="completed",
            goal_kind="lookup",
            expected_tools=["catalog.search"],
        )
        score = score_case(_run(plan=plan), gold)
        self.assertFalse(score.sequence_match)
        self.assertEqual(score.tool_precision, 0.0)
        self.assertEqual(score.tool_recall, 0.0)
        self.assertLess(score.ratio, 1.0)

    def test_blocked_case_skips_tool_metrics(self):
        gold = GoldCase(
            packet_id="P-2006",
            expected_status="blocked",
            expected_error_code="prompt_injection",
        )
        result = _run(
            packet_id="P-2006",
            status=RunStatus.BLOCKED,
            plan=None,
            approval=None,
            error_code="prompt_injection",
            attempts=0,
            tokens_planned=0,
            tokens_used=0,
            steps_planned=0,
        )
        score = score_case(result, gold)
        self.assertIsNone(score.sequence_match)
        self.assertTrue(score.status_match)
        self.assertEqual(score.ratio, 1.0)

    def test_gold_from_dict(self):
        gold = gold_from_dict(
            {
                "packet_id": "P-2001",
                "expected_status": "completed",
                "goal_kind": "lookup",
                "expected_tools": ["catalog.search"],
            }
        )
        self.assertEqual(gold.expected_tools, ["catalog.search"])

    def test_aggregate_report_rates(self):
        gold = [
            GoldCase("P-2001", "completed", goal_kind="lookup", expected_tools=["catalog.search"]),
            GoldCase("P-2008", "completed", goal_kind="lookup", expected_tools=["catalog.get"]),
        ]
        results = [
            _run(),
            _run(
                packet_id="P-2008",
                plan=parse_tool_plan(
                    helpers.valid_plan_json(
                        budget_tokens=10,
                        steps=[{"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-999"}, "bind": {}}],
                    )
                ),
            ),
        ]
        report = evaluate(results, gold)
        self.assertEqual(report["mean_score"], 1.0)
        self.assertEqual(report["sequence_accuracy"], 1.0)
        self.assertEqual(report["tool_precision"], 1.0)
        self.assertEqual(report["missing_results"], [])
