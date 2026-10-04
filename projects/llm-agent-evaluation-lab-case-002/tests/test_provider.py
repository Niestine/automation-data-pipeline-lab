import json
import unittest

from brief_router_lab.contracts import parse_tool_plan
from brief_router_lab.models import CompletionRequest
from brief_router_lab.provider import FakePlanner, HeuristicPlanner, ProviderError
import helpers


def _complete(packet) -> list[str]:
    request = CompletionRequest(
        task_id=packet.packet_id,
        system="sys",
        user=packet.canonical_json(),
    )
    text = HeuristicPlanner().complete(request).text
    plan = parse_tool_plan(text)
    return plan.tool_names()


class ProviderTests(unittest.TestCase):
    def test_heuristic_search_limit_and_query(self):
        packet = helpers.make_packet()
        request = CompletionRequest(task_id=packet.packet_id, system="sys", user=packet.canonical_json())
        plan = parse_tool_plan(HeuristicPlanner().complete(request).text)
        self.assertEqual(plan.goal_kind, "lookup")
        self.assertEqual(plan.steps[0].args["query"], "weekly recap")
        self.assertEqual(plan.steps[0].args["limit"], 3)

    def test_heuristic_publish_and_schedule_routes(self):
        publish = helpers.make_packet(
            packet_id="P-2003",
            operator_role="publisher",
            workspace="internal",
            goal="Publish a recap for AST-101 after review.",
        )
        schedule = helpers.make_packet(
            packet_id="P-2010",
            operator_role="editor",
            workspace="internal",
            goal="Hold SLOT-A for a recap of AST-101.",
        )
        self.assertEqual(
            _complete(publish),
            ["catalog.get", "draft.compose", "review.submit", "publish.queue"],
        )
        self.assertEqual(_complete(schedule), ["catalog.get", "draft.compose", "calendar.hold"])

    def test_fake_plan_object_and_text(self):
        planner = FakePlanner(
            {
                "P-a": [{"plan": helpers.valid_plan_dict()}],
                "P-b": [{"text": helpers.valid_plan_json()}],
            }
        )
        a = planner.complete(CompletionRequest(task_id="P-a", system="s", user="{}"))
        b = planner.complete(CompletionRequest(task_id="P-b", system="s", user="{}"))
        self.assertEqual(parse_tool_plan(a.text).goal_kind, "lookup")
        self.assertEqual(parse_tool_plan(b.text).goal_kind, "lookup")

    def test_fake_missing_script(self):
        planner = FakePlanner({})
        with self.assertRaises(ProviderError) as ctx:
            planner.complete(CompletionRequest(task_id="missing", system="s", user="{}"))
        self.assertEqual(ctx.exception.code, "no_script")

    def test_heuristic_rejects_non_json_user(self):
        with self.assertRaises(ProviderError) as ctx:
            HeuristicPlanner().complete(CompletionRequest(task_id="P", system="s", user="not-json"))
        self.assertEqual(ctx.exception.code, "bad_request")

    def test_request_user_is_canonical_packet_json(self):
        packet = helpers.make_packet()
        parsed = json.loads(packet.canonical_json())
        self.assertEqual(parsed["packet_id"], "P-2001")
        self.assertEqual(parsed["max_steps"], 8)
