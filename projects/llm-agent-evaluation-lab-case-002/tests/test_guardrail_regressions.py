"""Regression tests for guardrail gaps found in review.

Each test reproduces a bypass or failure mode the router previously allowed.
"""

import json
import unittest

import helpers
from brief_router_lab.contracts import parse_tool_plan
from brief_router_lab.models import RunStatus
from brief_router_lab.policy import admit_plan, inspect_plan_text
from brief_router_lab.provider import FakePlanner
from brief_router_lab.retry import CircuitBreaker
from brief_router_lab.workspace import BriefWorkspace, ToolError


def _compose(step_id, source_ids=("AST-101",), notes="from AST-101"):
    return {
        "id": step_id,
        "tool": "draft.compose",
        "args": {"title": "Recap brief", "source_ids": list(source_ids), "notes": notes},
        "bind": {},
    }


def _admit(plan_dict, packet):
    plan = parse_tool_plan(json.dumps(plan_dict))
    return admit_plan(plan, packet, BriefWorkspace().classification)


def _editor(**overrides):
    return helpers.make_packet(operator_role="editor", workspace="internal", **overrides)


class SearchClearanceTests(unittest.TestCase):
    def _search(self, workspace_name):
        plan = helpers.valid_plan_dict(
            steps=[{"id": "s1", "tool": "catalog.search", "args": {"query": "launch", "limit": 3}, "bind": {}}]
        )
        planner = helpers.scripted_planner("P-s", [{"plan": plan}])
        router, _, _ = helpers.make_router(planner)
        packet = helpers.make_packet(packet_id="P-s", workspace=workspace_name, goal="Search launch clips")
        return router.run(packet)

    def test_public_search_hides_restricted_titles(self):
        result = self._search("public")
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.step_results[0].payload, {"hits": [], "count": 0})

    def test_restricted_search_sees_restricted_titles(self):
        result = self._search("restricted")
        self.assertEqual(result.step_results[0].payload["hits"][0]["asset_id"], "AST-103")

    def test_kb_search_filters_by_clearance(self):
        workspace = BriefWorkspace()
        public = workspace.invoke("P", "kb.search", {"query": "publish"}, dry_run=False, clearance="public")
        restricted = workspace.invoke("P", "kb.search", {"query": "publish"}, dry_run=False, clearance="restricted")
        self.assertEqual(public["count"], 0)
        self.assertEqual([hit["note_id"] for hit in restricted["hits"]], ["NOTE-30"])

    def test_invoke_defaults_to_public_clearance(self):
        payload = BriefWorkspace().invoke("P", "catalog.search", {"query": "cut", "limit": 5}, dry_run=False)
        self.assertEqual(payload["count"], 0)


class DraftProvenanceTests(unittest.TestCase):
    def test_static_draft_id_is_denied(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=90,
            steps=[
                _compose("s1"),
                {"id": "s2", "tool": "review.submit", "args": {"draft_id": "DRF-other"}, "bind": {}},
            ],
        )
        self.assertEqual(_admit(plan, _editor()).code, "draft_provenance")

    def test_draft_id_bound_from_non_compose_step_is_denied(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=100,
            steps=[
                {"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}},
                _compose("s2"),
                {"id": "s3", "tool": "review.submit", "args": {}, "bind": {"draft_id": "$s1.asset_id"}},
            ],
        )
        self.assertEqual(_admit(plan, _editor()).code, "draft_provenance")

    def test_internal_publisher_cannot_queue_restricted_draft(self):
        restricted_plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=80,
            steps=[_compose("s1", source_ids=["AST-103"], notes="embargoed")],
        )
        hijack_plan = helpers.valid_plan_dict(
            goal_kind="publish",
            budget_tokens=125,
            steps=[
                {"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}},
                _compose("s2"),
                {"id": "s3", "tool": "review.submit", "args": {"draft_id": "DRF-R"}, "bind": {}},
                {"id": "s4", "tool": "publish.queue", "args": {"draft_id": "DRF-R"}, "bind": {}},
            ],
        )
        planner = FakePlanner({"R": [{"plan": restricted_plan}], "X": [{"plan": hijack_plan}]})
        workspace = BriefWorkspace()
        router, _, _ = helpers.make_router(planner, workspace=workspace)
        first = router.run(
            helpers.make_packet(packet_id="R", operator_role="editor", workspace="restricted", goal="Draft")
        )
        self.assertEqual(first.status, RunStatus.COMPLETED)
        second = router.run(
            helpers.make_packet(packet_id="X", operator_role="publisher", workspace="internal", goal="Publish")
        )
        self.assertEqual(second.status, RunStatus.DENIED)
        self.assertEqual(second.error_code, "draft_provenance")
        self.assertEqual(workspace.publish_queue, [])
        self.assertEqual(workspace.drafts["DRF-R"]["status"], "draft")

    def test_compose_conflict_for_reused_packet_id(self):
        workspace = BriefWorkspace()
        args = {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"}
        workspace.invoke("P-1", "draft.compose", args, dry_run=False)
        replay = workspace.invoke("P-1", "draft.compose", dict(args), dry_run=False)
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ToolError) as ctx:
            workspace.invoke("P-1", "draft.compose", {**args, "source_ids": ["AST-102"]}, dry_run=False)
        self.assertEqual(ctx.exception.code, "draft_conflict")
        self.assertEqual(workspace.drafts["DRF-P-1"]["source_ids"], ["AST-101"])


class SourceAndArgumentTests(unittest.TestCase):
    def test_unknown_catalog_source_is_denied(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=80,
            steps=[_compose("s1", source_ids=["AST-999"])],
        )
        self.assertEqual(_admit(plan, _editor()).code, "unknown_source")

    def test_non_catalog_source_id_fails_schema(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=80,
            steps=[_compose("s1", source_ids=["made-up"])],
        )
        self.assertEqual(_admit(plan, _editor()).code, "tool_schema")

    def test_arg_and_bind_overlap_is_denied(self):
        plan = helpers.valid_plan_dict(
            budget_tokens=30,
            steps=[
                {"id": "s1", "tool": "catalog.search", "args": {"query": "weekly recap", "limit": 3}, "bind": {}},
                {
                    "id": "s2",
                    "tool": "catalog.get",
                    "args": {"asset_id": "AST-101"},
                    "bind": {"asset_id": "$s1.hits[0].asset_id"},
                },
            ],
        )
        admission = _admit(plan, _editor())
        self.assertEqual(admission.code, "tool_schema")
        self.assertIn("both args and bind", admission.message)

    def test_cite_unknown_note_fails_even_in_dry_run(self):
        with self.assertRaises(ToolError) as ctx:
            BriefWorkspace().invoke("P", "draft.cite", {"draft_id": "DRF-P", "note_id": "NOTE-99"}, dry_run=True)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_credential_in_step_args_is_output_leak(self):
        plan = parse_tool_plan(
            helpers.valid_plan_json(
                goal_kind="research_brief",
                budget_tokens=80,
                steps=[_compose("s1", notes="password=hunter2")],
            )
        )
        self.assertEqual([item.code for item in inspect_plan_text(plan)], ["output_credential_leak"])

    def test_credential_in_step_args_blocks_run(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=80,
            steps=[_compose("s1", notes="api_key rotation")],
        )
        workspace = BriefWorkspace()
        router, _, _ = helpers.make_router(helpers.scripted_planner("P-leak", [{"plan": plan}]), workspace=workspace)
        result = router.run(_editor(packet_id="P-leak", goal="Draft a recap"))
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.error_code, "output_credential_leak")
        self.assertEqual(workspace.mutations, [])

    def test_workspace_rejects_unclassified_records(self):
        with self.assertRaises(ValueError):
            BriefWorkspace(assets={"AST-1": {"title": "no label"}})
        with self.assertRaises(ValueError):
            BriefWorkspace(notes={"NOTE-1": {"title": "x", "classification": "secret"}})


class ExecutionResilienceTests(unittest.TestCase):
    @staticmethod
    def _hold_plan():
        return helpers.valid_plan_dict(
            goal_kind="schedule",
            budget_tokens=90,
            steps=[
                _compose("s1"),
                {
                    "id": "s2",
                    "tool": "calendar.hold",
                    "args": {"slot_id": "SLOT-A"},
                    "bind": {"draft_id": "$s1.draft_id"},
                },
            ],
        )

    @staticmethod
    def _taken_slot_workspace():
        return BriefWorkspace(
            slots={"SLOT-A": {"at": "2026-10-06T10:00:00Z", "available": False, "held_by": "DRF-someone"}}
        )

    def test_business_errors_do_not_open_circuit(self):
        workspace = self._taken_slot_workspace()
        planner = FakePlanner({f"P-{n}": [{"plan": self._hold_plan()}] for n in range(4)})
        breaker = CircuitBreaker(failure_threshold=3, cooldown_ms=500)
        router, _, _ = helpers.make_router(planner, workspace=workspace, breaker=breaker)
        codes = [router.run(_editor(packet_id=f"P-{n}", goal="Hold")).error_code for n in range(4)]
        self.assertEqual(codes, ["slot_taken"] * 4)
        self.assertFalse(breaker.is_open("calendar.hold"))

    def test_rerun_after_failure_replays_completed_writes(self):
        workspace = self._taken_slot_workspace()
        planner = helpers.scripted_planner("P-r", [{"plan": self._hold_plan()}])
        router, _, _ = helpers.make_router(planner, workspace=workspace)
        packet = _editor(packet_id="P-r", goal="Hold")
        first = router.run(packet)
        self.assertEqual(first.status, RunStatus.FAILED)
        self.assertEqual(len(router.store), 0)
        workspace.slots["SLOT-A"].update(available=True, held_by=None)
        second = router.run(packet)
        self.assertEqual(second.status, RunStatus.COMPLETED)
        self.assertTrue(second.step_results[0].replayed)
        self.assertFalse(second.step_results[1].replayed)
        self.assertEqual([item[0] for item in workspace.mutations], ["compose", "hold"])


if __name__ == "__main__":
    unittest.main()
