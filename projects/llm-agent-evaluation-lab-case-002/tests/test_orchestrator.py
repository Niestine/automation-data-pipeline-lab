import unittest

import helpers
from brief_router_lab.models import RunStatus
from brief_router_lab.retry import CircuitBreaker, RetryPolicy
from brief_router_lab.workspace import BriefWorkspace, ToolFaults


class OrchestratorTests(unittest.TestCase):
    def test_search_completes_and_caches(self):
        planner = helpers.scripted_planner("P-2001", [{"plan": helpers.valid_plan_dict()}])
        router, _, logger = helpers.make_router(planner)
        packet = helpers.make_packet()
        first = router.run(packet)
        second = router.run(packet)
        self.assertEqual(first.status, RunStatus.COMPLETED)
        self.assertEqual(first.step_results[0].payload["hits"][0]["asset_id"], "AST-101")
        self.assertTrue(second.cached)
        self.assertEqual(len(planner.call_log), 1)
        self.assertIn("cache_hit", [item["event"] for item in logger.events])

    def test_dry_and_live_are_separate_caches(self):
        planner = helpers.scripted_planner("P-2001", [{"plan": helpers.valid_plan_dict()}])
        router, _, _ = helpers.make_router(planner)
        packet = helpers.make_packet()
        dry = router.run(packet, dry_run=True)
        live = router.run(packet, dry_run=False)
        self.assertEqual(dry.status, RunStatus.DRY_RUN)
        self.assertEqual(live.status, RunStatus.COMPLETED)
        self.assertFalse(live.cached)
        self.assertEqual(len(planner.call_log), 2)

    def test_failed_plan_is_not_cached(self):
        planner = helpers.scripted_planner(
            "P-2001",
            [{"error": True, "code": "timeout", "transient": True, "message": "blip"}],
        )
        router, sleeper, _ = helpers.make_router(planner, policy=RetryPolicy(max_attempts=2, jitter_ms=0))
        packet = helpers.make_packet()
        result = router.run(packet)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, "timeout")
        self.assertEqual(len(router.store), 0)
        self.assertTrue(sleeper.delays)

    def test_parse_error_then_valid_plan(self):
        planner = helpers.scripted_planner(
            "P-2001",
            [{"text": "not-json"}, {"plan": helpers.valid_plan_dict()}],
        )
        router, sleeper, _ = helpers.make_router(planner)
        result = router.run(helpers.make_packet())
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.attempts, 2)
        self.assertTrue(sleeper.delays)

    def test_schema_error_then_valid_plan(self):
        bad = helpers.valid_plan_dict()
        bad["goal_kind"] = "not-a-kind"
        planner = helpers.scripted_planner(
            "P-2001",
            [{"plan": bad}, {"plan": helpers.valid_plan_dict()}],
        )
        router, _, _ = helpers.make_router(planner)
        result = router.run(helpers.make_packet())
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.attempts, 2)

    def test_auth_error_does_not_retry(self):
        planner = helpers.scripted_planner(
            "P-2001",
            [{"error": True, "code": "auth", "transient": False, "message": "nope"}],
        )
        router, sleeper, _ = helpers.make_router(planner)
        result = router.run(helpers.make_packet())
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(sleeper.delays, [])

    def test_role_denied_does_not_mutate(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=90,
            steps=[
                {"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}},
                {
                    "id": "s2",
                    "tool": "draft.compose",
                    "args": {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
                    "bind": {},
                },
            ],
        )
        planner = helpers.scripted_planner("P-2004", [{"plan": plan}])
        workspace = BriefWorkspace()
        router, _, _ = helpers.make_router(planner, workspace=workspace)
        packet = helpers.make_packet(
            packet_id="P-2004",
            operator_role="viewer",
            workspace="public",
            goal="Draft a recap brief from AST-101.",
        )
        result = router.run(packet)
        self.assertEqual(result.status, RunStatus.DENIED)
        self.assertEqual(result.error_code, "role_denied")
        self.assertEqual(workspace.mutations, [])
        self.assertEqual(result.step_results, [])

    def test_pending_approval_then_approve_executes(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            confidence=0.4,
            needs_human=True,
            budget_tokens=90,
            steps=[
                {"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}},
                {
                    "id": "s2",
                    "tool": "draft.compose",
                    "args": {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
                    "bind": {},
                },
            ],
        )
        planner = helpers.scripted_planner("P-low", [{"plan": plan}])
        workspace = BriefWorkspace()
        router, _, _ = helpers.make_router(planner, workspace=workspace)
        packet = helpers.make_packet(
            packet_id="P-low",
            operator_role="editor",
            workspace="internal",
            goal="Draft a recap brief from AST-101.",
        )
        held = router.run(packet)
        self.assertEqual(held.status, RunStatus.PENDING_APPROVAL)
        self.assertEqual(workspace.drafts, {})
        approved = router.run(packet, approve=True)
        self.assertEqual(approved.status, RunStatus.COMPLETED)
        self.assertIn("DRF-P-low", workspace.drafts)

    def test_bind_from_prior_search_hit(self):
        plan = helpers.valid_plan_dict(
            budget_tokens=30,
            steps=[
                {"id": "s1", "tool": "catalog.search", "args": {"query": "weekly recap", "limit": 3}, "bind": {}},
                {"id": "s2", "tool": "catalog.get", "args": {}, "bind": {"asset_id": "$s1.hits[0].asset_id"}},
            ],
        )
        planner = helpers.scripted_planner("P-bind", [{"plan": plan}])
        router, _, _ = helpers.make_router(planner)
        packet = helpers.make_packet(packet_id="P-bind", operator_role="editor", workspace="internal")
        result = router.run(packet)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.step_results[1].payload["asset_id"], "AST-101")

    def test_empty_hits_bind_fails_and_is_not_cached(self):
        plan = helpers.valid_plan_dict(
            budget_tokens=30,
            steps=[
                {"id": "s1", "tool": "catalog.search", "args": {"query": "no-such-clip", "limit": 3}, "bind": {}},
                {"id": "s2", "tool": "catalog.get", "args": {}, "bind": {"asset_id": "$s1.hits[0].asset_id"}},
            ],
        )
        planner = helpers.scripted_planner("P-miss", [{"plan": plan}])
        router, _, _ = helpers.make_router(planner)
        packet = helpers.make_packet(packet_id="P-miss", operator_role="editor", workspace="internal")
        result = router.run(packet)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error_code, "bind_path")
        self.assertEqual(len(router.store), 0)

    def test_tool_transient_then_success(self):
        plan = helpers.valid_plan_dict(
            budget_tokens=10,
            steps=[{"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}}],
        )
        planner = helpers.scripted_planner("P-flaky", [{"plan": plan}])
        faults = ToolFaults(
            {
                "P-flaky": [
                    {"tool": "catalog.get", "error": True, "code": "timeout", "transient": True},
                ]
            }
        )
        workspace = BriefWorkspace(faults=faults)
        router, sleeper, _ = helpers.make_router(planner, workspace=workspace)
        packet = helpers.make_packet(packet_id="P-flaky", operator_role="editor", workspace="internal")
        result = router.run(packet)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.step_results[0].attempts, 2)
        self.assertTrue(sleeper.delays)

    def test_circuit_opens_across_packets(self):
        plan = helpers.valid_plan_dict(
            budget_tokens=10,
            steps=[{"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}}],
        )
        planner = helpers.scripted_planner(
            "P-a",
            [{"plan": plan}],
            **{"P-b": [{"plan": plan}]},
        )
        faults = ToolFaults(
            {
                "P-a": [
                    {"tool": "catalog.get", "error": True, "code": "timeout", "transient": True},
                    {"tool": "catalog.get", "error": True, "code": "timeout", "transient": True},
                    {"tool": "catalog.get", "error": True, "code": "timeout", "transient": True},
                ]
            }
        )
        workspace = BriefWorkspace(faults=faults)
        breaker = CircuitBreaker(failure_threshold=3, cooldown_ms=500)
        router, _, _ = helpers.make_router(planner, workspace=workspace, breaker=breaker)
        first = router.run(helpers.make_packet(packet_id="P-a", operator_role="editor", workspace="internal"))
        self.assertEqual(first.status, RunStatus.FAILED)
        self.assertEqual(first.error_code, "timeout")
        second = router.run(helpers.make_packet(packet_id="P-b", operator_role="editor", workspace="internal"))
        self.assertEqual(second.status, RunStatus.FAILED)
        self.assertEqual(second.error_code, "circuit_open")

    def test_dry_run_write_skips_mutations(self):
        plan = helpers.valid_plan_dict(
            goal_kind="research_brief",
            budget_tokens=90,
            steps=[
                {"id": "s1", "tool": "catalog.get", "args": {"asset_id": "AST-101"}, "bind": {}},
                {
                    "id": "s2",
                    "tool": "draft.compose",
                    "args": {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
                    "bind": {},
                },
            ],
        )
        planner = helpers.scripted_planner("P-dry", [{"plan": plan}])
        workspace = BriefWorkspace()
        router, _, _ = helpers.make_router(planner, workspace=workspace)
        packet = helpers.make_packet(
            packet_id="P-dry",
            operator_role="editor",
            workspace="internal",
            goal="Draft a recap brief from AST-101.",
        )
        result = router.run(packet, dry_run=True)
        self.assertEqual(result.status, RunStatus.DRY_RUN)
        self.assertEqual(workspace.drafts, {})
        self.assertEqual(workspace.mutations, [])
        self.assertTrue(result.step_results[1].payload.get("would_mutate"))

    def test_force_bypasses_cache(self):
        planner = helpers.scripted_planner("P-2001", [{"plan": helpers.valid_plan_dict()}])
        router, _, _ = helpers.make_router(planner)
        packet = helpers.make_packet()
        router.run(packet)
        router.run(packet, force=True)
        self.assertEqual(len(planner.call_log), 2)
