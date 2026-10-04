import tempfile
import unittest

import helpers  # noqa: F401

from automation_job_lab.errors import StateError, ValidationError
from automation_job_lab.store import Workspace


class StoreTests(unittest.TestCase):
    def test_upsert_insert_and_duplicate(self):
        ws = Workspace()
        row = {"id": "REC-1001", "version": 1, "kind": "ticket"}
        self.assertEqual(ws.upsert("inbox", row), "inserted")
        self.assertEqual(ws.upsert("inbox", dict(row)), "ignored_duplicate")
        newer = dict(row)
        newer["version"] = 2
        self.assertEqual(ws.upsert("inbox", newer), "updated")
        stale = dict(row)
        stale["version"] = 1
        self.assertEqual(ws.upsert("inbox", stale), "ignored_stale")

    def test_equal_version_different_payload_is_conflict(self):
        ws = Workspace()
        ws.upsert("inbox", {"id": "REC-1001", "version": 1, "kind": "ticket"})
        outcome = ws.upsert("inbox", {"id": "REC-1001", "version": 1, "kind": "report"})
        self.assertEqual(outcome, "ignored_conflict")
        self.assertEqual(ws.get("inbox", "REC-1001")["kind"], "ticket")

    def test_dry_run_overlay_is_visible_then_discardable(self):
        ws = Workspace()
        ws.upsert("inbox", {"id": "REC-1001", "version": 1})
        ws.upsert("staging", {"id": "REC-1001", "version": 1}, dry_run=True)
        self.assertEqual(len(ws.list("staging")), 1)
        self.assertEqual(ws.records["staging"], {})
        ws.delete("inbox", "REC-1001", dry_run=True)
        self.assertEqual(ws.list("inbox"), [])
        self.assertIn("REC-1001", ws.records["inbox"])
        ws.discard_staged()
        self.assertEqual(len(ws.list("inbox")), 1)
        self.assertEqual(ws.list("staging"), [])

    def test_file_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/workspace.json"
            ws = Workspace(path)
            ws.upsert("inbox", {"id": "REC-1001", "version": 1})
            ws.put_blob("exports", "report.json", {"id": "report.json", "n": 1})
            loaded = Workspace(path)
            self.assertEqual(loaded.get("inbox", "REC-1001")["id"], "REC-1001")
            self.assertEqual(loaded.get("exports", "report.json")["n"], 1)

    def test_corrupt_workspace_raises_state_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/workspace.json"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(StateError):
                Workspace(path)

    def test_unknown_bucket_is_rejected(self):
        ws = Workspace()
        with self.assertRaises(ValidationError):
            ws.list("nope")
