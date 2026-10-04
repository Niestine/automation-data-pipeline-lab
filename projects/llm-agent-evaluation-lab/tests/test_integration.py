import json
import unittest

import helpers
from llm_agent_lab.evaluation import evaluate, load_gold, load_tickets
from llm_agent_lab.orchestrator import AgentOrchestrator
from llm_agent_lab.provider import FakeProvider, HeuristicProvider
from llm_agent_lab.store import RunStore
from llm_agent_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper
from llm_agent_lab.tools import RecordStore


def _run_batch(provider):
    tickets = load_tickets(json.loads((helpers.EXAMPLES / "tickets.json").read_text(encoding="utf-8")))
    gold = load_gold(json.loads((helpers.EXAMPLES / "gold_labels.json").read_text(encoding="utf-8")))
    records = RecordStore(
        json.loads((helpers.EXAMPLES / "records.json").read_text(encoding="utf-8"))
    )
    clock = ManualClock()
    orch = AgentOrchestrator(
        provider,
        records=records,
        store=RunStore(),
        logger=JsonLogger(),
        clock=clock,
        sleeper=RecordingSleeper(clock),
        seed=7,
    )
    results = [orch.run(ticket) for ticket in tickets]
    return results, gold, records, orch


class IntegrationTests(unittest.TestCase):
    def test_heuristic_provider_matches_gold_set(self):
        results, gold, records, _ = _run_batch(HeuristicProvider())
        report = evaluate(results, gold)
        self.assertEqual(report["missing_results"], [])
        self.assertEqual(report["status_accuracy"], 1.0)
        self.assertEqual(report["intent_accuracy"], 1.0)
        self.assertEqual(report["action_accuracy"], 1.0)
        self.assertEqual(report["approval_accuracy"], 1.0)
        self.assertEqual(report["mean_score"], 1.0)
        self.assertEqual(records.records["ORD-200"]["status"], "pending")
        self.assertEqual(records.mutations, [])
        blocked = [item for item in results if item.status == "blocked"]
        self.assertTrue(all(item.attempts == 0 for item in blocked))

    def test_fake_provider_matches_gold_set(self):
        scripts = json.loads((helpers.EXAMPLES / "provider_script.json").read_text(encoding="utf-8"))
        results, gold, records, orch = _run_batch(FakeProvider(scripts))
        report = evaluate(results, gold)
        self.assertEqual(report["status_accuracy"], 1.0)
        self.assertEqual(report["mean_score"], 1.0)
        self.assertEqual(records.mutations, [])
        call_ids = [item.task_id for item in orch.provider.call_log]
        self.assertNotIn("T-1005", call_ids)
        self.assertNotIn("T-1006", call_ids)
        self.assertNotIn("T-1010", call_ids)

    def test_evaluator_detects_degraded_provider(self):
        scripts = json.loads((helpers.EXAMPLES / "provider_script.json").read_text(encoding="utf-8"))
        # Regressions: wrong intent/action on one ticket, permanently bad JSON on another.
        scripts["T-1002"] = scripts["T-1001"]
        scripts["T-1007"] = [{"text": "Sure, escalating now."}]
        results, gold, _, _ = _run_batch(FakeProvider(scripts))
        report = evaluate(results, gold)
        self.assertLess(report["mean_score"], 1.0)
        self.assertLess(report["intent_accuracy"], 1.0)
        self.assertLess(report["schema_validity_rate"], 1.0)
        self.assertGreater(report["retry_rate"], 0.0)
        failing = {row["ticket_id"] for row in report["scores"] if row["ratio"] < 1.0}
        self.assertEqual(failing, {"T-1002", "T-1007"})

    def test_unknown_role_is_rejected_before_run(self):
        with self.assertRaises(ValueError):
            helpers.make_ticket(requester_role="superuser")


if __name__ == "__main__":
    unittest.main()
