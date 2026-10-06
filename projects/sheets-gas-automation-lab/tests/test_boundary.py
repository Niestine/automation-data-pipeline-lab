"""Deployment text, the Apps Script example, and the offline network guard."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

import support  # noqa: F401

from sessionfee.commit import commit_week
from sessionfee.fixtures import PROJECT_ROOT, decimal_field, load_json, sessions_from_document
from sessionfee.idempotency import Ledger
from sessionfee.lockstub import LockStub
from sessionfee.measure import run_controls
from sessionfee.plan import MemorySheet

BOUNDARY = PROJECT_ROOT / "DEPLOYMENT_BOUNDARY.md"
GAS = PROJECT_ROOT / "gas" / "Code.gs"

REQUIRED_SENTENCES = (
    "All quotas are subject to elimination, reduction, or change at any time, without notice.",
    "Quotas are per user and reset 24 hours after the first request.",
    "Script executions and API requests don't cause triggers to run.",
    "Installable triggers always run under the account of the person who created them.",
    "The time might be slightly randomized",
    "no error message appears on your screen",
)


class BoundaryTests(unittest.TestCase):
    def test_boundary_document_contains_quota_and_trigger_sentences(self) -> None:
        text = BOUNDARY.read_text(encoding="utf-8")
        for sentence in REQUIRED_SENTENCES:
            self.assertIn(sentence, text)
        self.assertIn("6 min / execution", text)
        self.assertEqual(text.count("6 min / execution"), 2)
        self.assertIn("90 min / day", text)
        self.assertIn("6 hr / day", text)
        self.assertIn("20 / user / script", text)
        self.assertIn("9 KB / val", text)
        self.assertIn("500 KB / property store", text)
        self.assertIn("This package does not call Google.", text)

    def test_apps_script_example_try_lock_flush_before_release(self) -> None:
        text = GAS.read_text(encoding="utf-8")
        self.assertIn("LockService.getScriptLock", text)
        self.assertIn("tryLock", text)
        self.assertIn("SpreadsheetApp.flush", text)
        self.assertIn("releaseLock", text)
        self.assertLess(text.index("SpreadsheetApp.flush"), text.index("releaseLock"))
        self.assertLess(text.index("if (!acquired)"), text.index("writeCanonicalRow_"))
        self.assertNotIn("UrlFetchApp", text)
        self.assertNotIn("newTrigger", text)
        self.assertNotIn("PropertiesService", text)
        self.assertNotIn("ScriptApp", text)
        finally_block = text[text.index("} finally {"):]
        self.assertLess(finally_block.index("SpreadsheetApp.flush()"), finally_block.index("lock.releaseLock()"))
        writer = text[text.index("function writeCanonicalRow_"):]
        self.assertNotIn(".appendRow(", writer)
        self.assertLess(writer.index("setNumberFormat('@')"), writer.index(".setValues("))

    def test_representative_run_opens_no_socket(self) -> None:
        document = load_json("week_sessions.json")

        def blocked(*_args, **_kwargs):
            raise AssertionError("network")

        with patch("socket.create_connection", blocked), patch("socket.getaddrinfo", blocked):
            run_controls()
            commit_week(
                MemorySheet(row_version=0),
                Ledger(),
                LockStub(),
                sessions_from_document(document),
                expected_total=decimal_field(document, "expected_total_jpy"),
                expected_row_count=int(document["expected_row_count"]),
                read_version=0,
                dry_run=True,
            )
        self.assertTrue(callable(socket.create_connection))


class MeasureReportTests(unittest.TestCase):
    def test_measure_report_matches_the_direct_fixture_a_split(self) -> None:
        from sessionfee.fixtures import rows_from_document
        from sessionfee.invariants import check_invariant
        from sessionfee.schema import check_rows

        report = run_controls()
        document = load_json("fixture_a_cell_valid_wrong_total.json")
        rows = rows_from_document(document)
        schema_pass = check_rows(rows).passed
        invariant_pass = check_invariant(
            written_total=decimal_field(document, "written_total_jpy"),
            expected_total=decimal_field(document, "expected_total_jpy"),
            written_row_count=len(rows),
            expected_row_count=int(document["expected_row_count"]),
        ).passed
        fixture_a = report["fixture_a"]
        self.assertIsInstance(fixture_a, dict)
        self.assertEqual(fixture_a["schema_pass"], schema_pass)
        self.assertEqual(fixture_a["invariant_pass"], invariant_pass)
        self.assertTrue(schema_pass)
        self.assertFalse(invariant_pass)
        formulas = report["formulas"]
        self.assertIsInstance(formulas, dict)
        self.assertTrue(formulas["sum_range_accepted"])
        self.assertFalse(formulas["if_accepted"])
        self.assertFalse(formulas["chain8_ceiling_accepted"])
        idempotency = report["idempotency"]
        self.assertIsInstance(idempotency, dict)
        self.assertEqual(idempotency["append_baseline_rows"], 2)
        self.assertEqual(idempotency["ledger_same_key_rows"], 1)
        self.assertEqual(idempotency["content_hash_two_keys_rows"], 1)
        self.assertEqual(idempotency["ledger_two_keys_rows"], 2)
        commit = report["commit"]
        self.assertIsInstance(commit, dict)
        self.assertEqual(commit["batch_mixed_committed_cells"], 0)
        self.assertEqual(commit["separate_mixed_committed_cells"], 1)
        serials = report["serials"]
        self.assertIsInstance(serials, dict)
        self.assertEqual(serials["jan1_noon"], "2.5")
        self.assertEqual(serials["feb1_1500"], "33.625")
        self.assertEqual(serials["mar1_derived"], "61")
        self.assertEqual(serials["leap_bug_mar1"], "62")


if __name__ == "__main__":
    unittest.main()
