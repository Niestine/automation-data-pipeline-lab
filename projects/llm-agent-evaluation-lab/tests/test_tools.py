import unittest

import helpers  # noqa: F401  (adds src/ to sys.path)
from llm_agent_lab.models import Approval
from llm_agent_lab.tools import RecordStore, ToolExecutor


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.store = RecordStore()
        self.tools = ToolExecutor(self.store)

    def test_lookup_found_and_missing(self):
        found = self.tools.execute(
            "lookup_record",
            {"record_id": "ORD-100"},
            approval=Approval.AUTO_ALLOW,
            dry_run=False,
        )
        missing = self.tools.execute(
            "lookup_record",
            {"record_id": "ORD-999"},
            approval=Approval.AUTO_ALLOW,
            dry_run=False,
        )
        self.assertTrue(found.payload["found"])
        self.assertEqual(found.payload["record"]["status"], "shipped")
        self.assertFalse(missing.payload["found"])

    def test_update_mutates_store(self):
        result = self.tools.execute(
            "update_record",
            {"record_id": "ORD-200", "fields": {"status": "shipped"}},
            approval=Approval.AUTO_ALLOW,
            dry_run=False,
        )
        self.assertTrue(result.payload["updated"])
        self.assertEqual(self.store.records["ORD-200"]["status"], "shipped")
        self.assertEqual(len(self.store.mutations), 1)

    def test_dry_run_does_not_mutate(self):
        result = self.tools.execute(
            "update_record",
            {"record_id": "ORD-200", "fields": {"status": "shipped"}},
            approval=Approval.AUTO_ALLOW,
            dry_run=True,
        )
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(self.store.records["ORD-200"]["status"], "pending")
        self.assertEqual(self.store.mutations, [])

    def test_pending_approval_does_not_mutate(self):
        result = self.tools.execute(
            "update_record",
            {"record_id": "ORD-200", "fields": {"status": "shipped"}},
            approval=Approval.REQUIRE_APPROVAL,
            dry_run=False,
        )
        self.assertEqual(result.status, "pending_approval")
        self.assertEqual(self.store.records["ORD-200"]["status"], "pending")

    def test_export_rejects_full_scope(self):
        result = self.tools.execute(
            "export_data",
            {"scope": "full"},
            approval=Approval.AUTO_ALLOW,
            dry_run=False,
        )
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.payload["reason"], "export_scope_forbidden")

    def test_export_aggregate_counts_from_store(self):
        result = self.tools.execute(
            "export_data",
            {"scope": "aggregate_counts"},
            approval=Approval.AUTO_ALLOW,
            dry_run=False,
        )
        self.assertEqual(result.status, "completed")
        counts = {row["status"]: row["count"] for row in result.payload["rows"]}
        self.assertEqual(counts["shipped"], 1)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["blocked"], 1)

    def test_update_rejects_fields_outside_allowlist(self):
        result = self.tools.execute(
            "update_record",
            {"record_id": "ORD-100", "fields": {"status": "lost", "owner_email": "x@example.test"}},
            approval=Approval.AUTO_ALLOW,
            dry_run=False,
        )
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.payload["fields"], ["owner_email"])
        self.assertEqual(self.store.records["ORD-100"]["status"], "shipped")
        self.assertEqual(self.store.mutations, [])

    def test_update_with_missing_fields_fails(self):
        result = self.tools.execute(
            "update_record",
            {"record_id": "ORD-100"},
            approval=Approval.AUTO_ALLOW,
            dry_run=False,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.payload["reason"], "invalid_args")

    def test_deny_never_reaches_store(self):
        result = self.tools.execute(
            "update_record",
            {"record_id": "ORD-200", "fields": {"status": "shipped"}},
            approval=Approval.DENY,
            dry_run=False,
        )
        self.assertEqual(result.status, "denied")
        self.assertEqual(self.store.mutations, [])

    def test_default_store_is_not_shared_between_instances(self):
        RecordStore().update("ORD-100", {"status": "lost"})
        self.assertEqual(RecordStore().records["ORD-100"]["status"], "shipped")


if __name__ == "__main__":
    unittest.main()
