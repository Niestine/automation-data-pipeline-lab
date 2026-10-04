import unittest

from brief_router_lab.workspace import BriefWorkspace, ToolError, ToolFaults


class WorkspaceTests(unittest.TestCase):
    def test_search_matches_title(self):
        workspace = BriefWorkspace()
        payload = workspace.invoke(
            "P-2001",
            "catalog.search",
            {"query": "weekly recap", "limit": 3},
            dry_run=False,
        )
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["hits"][0]["asset_id"], "AST-101")

    def test_missing_asset_is_found_false(self):
        workspace = BriefWorkspace()
        payload = workspace.invoke("P-2008", "catalog.get", {"asset_id": "AST-999"}, dry_run=False)
        self.assertFalse(payload["found"])
        self.assertEqual(workspace.mutations, [])

    def test_compose_cite_submit_publish_live(self):
        workspace = BriefWorkspace()
        composed = workspace.invoke(
            "P-2003",
            "draft.compose",
            {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
            dry_run=False,
        )
        draft_id = composed["draft_id"]
        workspace.invoke("P-2003", "draft.cite", {"draft_id": draft_id, "note_id": "NOTE-10"}, dry_run=False)
        workspace.invoke("P-2003", "review.submit", {"draft_id": draft_id}, dry_run=False)
        queued = workspace.invoke("P-2003", "publish.queue", {"draft_id": draft_id}, dry_run=False)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(workspace.publish_queue[0]["draft_id"], draft_id)
        replay = workspace.invoke("P-2003", "publish.queue", {"draft_id": draft_id}, dry_run=False)
        self.assertTrue(replay.get("replayed"))

    def test_publish_without_submit_fails(self):
        workspace = BriefWorkspace()
        composed = workspace.invoke(
            "P-x",
            "draft.compose",
            {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
            dry_run=False,
        )
        with self.assertRaises(ToolError) as ctx:
            workspace.invoke("P-x", "publish.queue", {"draft_id": composed["draft_id"]}, dry_run=False)
        self.assertEqual(ctx.exception.code, "not_submitted")

    def test_dry_run_compose_does_not_store(self):
        workspace = BriefWorkspace()
        payload = workspace.invoke(
            "P-2002",
            "draft.compose",
            {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
            dry_run=True,
        )
        self.assertTrue(payload["would_mutate"])
        self.assertEqual(workspace.drafts, {})
        self.assertEqual(workspace.mutations, [])

    def test_calendar_hold_idempotent_for_same_draft(self):
        workspace = BriefWorkspace()
        composed = workspace.invoke(
            "P-2010",
            "draft.compose",
            {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
            dry_run=False,
        )
        first = workspace.invoke(
            "P-2010",
            "calendar.hold",
            {"slot_id": "SLOT-A", "draft_id": composed["draft_id"]},
            dry_run=False,
        )
        second = workspace.invoke(
            "P-2010",
            "calendar.hold",
            {"slot_id": "SLOT-A", "draft_id": composed["draft_id"]},
            dry_run=False,
        )
        self.assertTrue(first["held"])
        self.assertTrue(second.get("replayed"))

    def test_slot_taken_by_other_draft(self):
        workspace = BriefWorkspace()
        workspace.invoke(
            "P-2010",
            "draft.compose",
            {"title": "Recap brief", "source_ids": ["AST-101"], "notes": "from AST-101"},
            dry_run=False,
        )
        workspace.invoke("P-2010", "calendar.hold", {"slot_id": "SLOT-A", "draft_id": "DRF-P-2010"}, dry_run=False)
        with self.assertRaises(ToolError) as ctx:
            workspace.invoke("P-other", "calendar.hold", {"slot_id": "SLOT-A", "draft_id": "DRF-other"}, dry_run=False)
        self.assertEqual(ctx.exception.code, "slot_taken")

    def test_fault_script_raises_transient(self):
        faults = ToolFaults(
            {
                "P-x": [
                    {"tool": "catalog.get", "error": True, "code": "timeout", "transient": True},
                ]
            }
        )
        workspace = BriefWorkspace(faults=faults)
        with self.assertRaises(ToolError) as ctx:
            workspace.invoke("P-x", "catalog.get", {"asset_id": "AST-101"}, dry_run=False)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertTrue(ctx.exception.transient)
        payload = workspace.invoke("P-x", "catalog.get", {"asset_id": "AST-101"}, dry_run=False)
        self.assertTrue(payload["found"])
