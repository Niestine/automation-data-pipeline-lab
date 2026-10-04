import copy
import unittest

import helpers
from brief_router_lab.evaluation import evaluate, load_gold, load_packets
from brief_router_lab.provider import FakePlanner, HeuristicPlanner
from brief_router_lab.workspace import BriefWorkspace


def _run_suite(provider) -> dict:
    packets = load_packets(helpers.load_example("packets.json"))
    gold = load_gold(helpers.load_example("gold_labels.json"))
    workspace_payload = helpers.load_example("workspace.json")
    workspace = BriefWorkspace(
        assets=workspace_payload["assets"],
        notes=workspace_payload["notes"],
        slots=workspace_payload["slots"],
    )
    router, _, _ = helpers.make_router(provider, workspace=workspace)
    results = [router.run(packet) for packet in packets]
    return evaluate(results, gold)


class IntegrationTests(unittest.TestCase):
    def test_heuristic_matches_gold_set(self):
        report = _run_suite(HeuristicPlanner())
        self.assertEqual(report["mean_score"], 1.0)
        self.assertEqual(report["status_accuracy"], 1.0)
        self.assertEqual(report["sequence_accuracy"], 1.0)
        self.assertEqual(report["tool_precision"], 1.0)
        self.assertEqual(report["tool_recall"], 1.0)

    def test_fake_planner_script_matches_gold_set(self):
        script = helpers.load_example("planner_script.json")
        report = _run_suite(FakePlanner(script))
        self.assertEqual(report["mean_score"], 1.0)
        self.assertEqual(report["schema_validity_rate"], 1.0)

    def test_degraded_router_is_detected(self):
        script = copy.deepcopy(helpers.load_example("planner_script.json"))
        script["P-2001"][0]["plan"]["steps"] = [
            {"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}}
        ]
        script["P-2001"][0]["plan"]["budget_tokens"] = 10
        report = _run_suite(FakePlanner(script))
        self.assertLess(report["mean_score"], 1.0)
        self.assertLess(report["sequence_accuracy"], 1.0)
        p2001 = next(item for item in report["scores"] if item["packet_id"] == "P-2001")
        self.assertFalse(p2001["sequence_match"])

    def test_provider_requests_json_object_at_zero_temp(self):
        planner = FakePlanner(helpers.load_example("planner_script.json"))
        packets = load_packets(helpers.load_example("packets.json"))
        workspace_payload = helpers.load_example("workspace.json")
        workspace = BriefWorkspace(
            assets=workspace_payload["assets"],
            notes=workspace_payload["notes"],
            slots=workspace_payload["slots"],
        )
        router, _, _ = helpers.make_router(planner, workspace=workspace)
        router.run(packets[0])
        request = planner.call_log[0]
        self.assertEqual(request.response_format, "json_object")
        self.assertEqual(request.temperature, 0.0)
        self.assertEqual(request.response_schema_name, "tool_plan_v1")
        self.assertIn("tool_plan_v1", request.system)
