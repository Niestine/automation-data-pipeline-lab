"""Terminal ledger reward, format axes, and key order."""

from __future__ import annotations

import unittest

from helpers import manifest

from curbgate.desk import canonical_db, run_turns
from curbgate.models import GoldenCase
from curbgate.scoring import ScorerFault, parse_output, score_completion, score_tool_case


def _ledger():
    return {
        "permits": {
            "P-100": {"holder": "resident-17", "hold": False, "status": "active", "zone": "B"},
            "P-101": {"holder": "resident-18", "hold": False, "status": "active", "zone": "A"},
        }
    }


def _goal():
    goal = _ledger()
    goal["permits"]["P-100"]["hold"] = True
    return goal


def _case(**kwargs) -> GoldenCase:
    raw = {
        "id": "case",
        "cluster_id": "case",
        "template_id": "clerk.direct",
        "suite": "agent_state",
        "prompt_name": "clerk.issue",
        "schema_id": "curb.label_v1",
        "schema_key_order": ["answer"],
        "risk_tags": ["human-AI configuration"],
        "test_type": "mft",
        "capability": "agent",
        "expectation": {},
    }
    raw.update(kwargs)
    return GoldenCase.from_dict(raw)


class WorldTests(unittest.TestCase):
    def test_read_order_does_not_change_goal_success(self) -> None:
        ledger = _ledger()
        goal = _goal()
        writes = [
            {"user_text": "confirm hold"},
            {"tool_calls": [{"name": "place_hold", "arguments": {"permit_id": "P-100", "zone": "B"}}]},
            {"text": "Hold placed. Fee 12.00."},
        ]
        first = [{"tool_calls": [{"name": "lookup_permit", "arguments": {"permit_id": "P-100"}}]}] + writes
        second = [{"tool_calls": [{"name": "list_zone", "arguments": {"zone": "B"}}]}] + writes
        case = _case(
            expectation={
                "goal_db": goal,
                "required_substrings": ["12.00"],
                "confirmation_phrase": "confirm hold",
                "max_writes": 1,
            }
        )
        pinned = manifest()
        left, _, _ = score_tool_case(case, {"turns": first}, ledger, pinned, False)
        right, _, _ = score_tool_case(case, {"turns": second}, ledger, pinned, False)
        self.assertEqual(left["goal_state_match"], 1)
        self.assertEqual(right["goal_state_match"], 1)
        self.assertEqual(left["goal_success"], 1)
        self.assertEqual(right["goal_success"], 1)
        self.assertEqual(left["policy_ok"], 1)
        self.assertEqual(canonical_db(ledger), canonical_db(_ledger()))

    def test_extra_write_fails_goal_and_missing_confirmation_keeps_it(self) -> None:
        ledger = _ledger()
        goal = _goal()
        extra = [
            {"user_text": "confirm hold"},
            {"tool_calls": [{"name": "place_hold", "arguments": {"permit_id": "P-100", "zone": "B"}}]},
            {"tool_calls": [{"name": "place_hold", "arguments": {"permit_id": "P-101", "zone": "A"}}]},
            {"text": "Hold placed. Fee 12.00."},
        ]
        quiet = [
            {"tool_calls": [{"name": "place_hold", "arguments": {"permit_id": "P-100", "zone": "B"}}]},
            {"text": "Hold placed. Fee 12.00."},
        ]
        case = _case(
            expectation={
                "goal_db": goal,
                "required_substrings": ["12.00"],
                "confirmation_phrase": "confirm hold",
                "max_writes": 2,
            }
        )
        pinned = manifest()
        changed, _, _ = score_tool_case(case, {"turns": extra}, ledger, pinned, False)
        self.assertEqual(changed["goal_success"], 0)
        self.assertEqual(changed["goal_state_match"], 0)
        unconfirmed, _, _ = score_tool_case(case, {"turns": quiet}, ledger, pinned, False)
        self.assertEqual(unconfirmed["goal_success"], 1)
        self.assertEqual(unconfirmed["policy_ok"], 0)

    def test_action_cap_stops_before_the_write(self) -> None:
        turns = [{"tool_calls": [{"name": "lookup_permit", "arguments": {"permit_id": "P-100"}}]} for _ in range(9)]
        turns.append({"tool_calls": [{"name": "place_hold", "arguments": {"permit_id": "P-100", "zone": "B"}}]})
        episode = run_turns(turns, _ledger(), False, 8, False, "cap")
        self.assertTrue(episode.stopped_at_cap)
        self.assertEqual(episode.writes_applied, 0)
        self.assertEqual(canonical_db(episode.db), canonical_db(_ledger()))


class FormatTests(unittest.TestCase):
    def test_valid_json_with_wrong_answers_splits_the_axes(self) -> None:
        pinned = manifest()
        case = _case(
            suite="format_matrix",
            suite_family="reasoning",
            schema_id="curb.reason_v1",
            schema_key_order=["reason", "answer"],
            format_level="constrained",
            risk_tags=["information integrity"],
            expectation={"label": "issue"},
        )
        scores = score_completion(case, {"raw": '{"reason": "full", "answer": "refuse"}'}, pinned)
        self.assertEqual(scores["schema_valid"], 1)
        self.assertEqual(scores["task_correct"], 0)
        self.assertEqual(scores["key_order_ok"], 1)

    def test_key_order_fails_when_the_answer_is_still_correct(self) -> None:
        parsed = parse_output(
            '{"answer": "issue", "reason": "clear"}',
            "curb.reason_v1",
            ["reason", "answer"],
            "constrained",
        )
        self.assertEqual(parsed.answer, "issue")
        self.assertEqual(parsed.schema_valid, 1)
        self.assertEqual(parsed.key_order_ok, 0)
        self.assertEqual(parsed.observed_order, ["answer", "reason"])

    def test_parser_exception_is_a_harness_fault(self) -> None:
        with self.assertRaises(ScorerFault):
            parse_output('{"answer": "issue"}', "missing.schema", ["answer"], "constrained")

    def test_invalid_json_is_unscored_for_the_task(self) -> None:
        parsed = parse_output("{not json", "curb.label_v1", ["answer"], "loose")
        self.assertEqual(parsed.schema_valid, 0)
        self.assertIsNone(parsed.answer)

    def test_freeform_and_constrained_classification_can_disagree(self) -> None:
        pinned = manifest()
        free = _case(
            id="free",
            suite="format_matrix",
            suite_family="classification",
            format_level="free",
            risk_tags=["information integrity"],
            expectation={"label": "renew"},
        )
        constrained = _case(
            id="constrained",
            suite="format_matrix",
            suite_family="classification",
            format_level="constrained",
            risk_tags=["information integrity"],
            expectation={"label": "renew"},
        )
        free_scores = score_completion(free, {"raw": "Answer: refuse"}, pinned)
        held = score_completion(constrained, {"raw": '{"answer": "renew"}'}, pinned)
        self.assertEqual(free_scores["task_correct"], 0)
        self.assertIsNone(free_scores["schema_valid"])
        self.assertEqual(held["task_correct"], 1)
        self.assertEqual(held["schema_valid"], 1)
        self.assertNotEqual(free_scores["task_correct"], held["task_correct"])


if __name__ == "__main__":
    unittest.main()
