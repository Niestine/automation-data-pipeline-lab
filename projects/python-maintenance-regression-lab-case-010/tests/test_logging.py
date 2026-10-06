"""Failure and repair logs carry locations. Field text stays out of the line."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from wharf_sheet.recognize import hardened_parse, legacy_parse


class LoggingTests(unittest.TestCase):
    def test_clean_success_logs_nothing(self):
        with self.assertNoLogs("wharf_sheet", level="DEBUG"):
            result = hardened_parse(b"a,b\r\n")
        self.assertEqual(result.records, (("a", "b"),))
        self.assertEqual(result.events, ())

    def test_failure_log_names_the_decision_and_omits_the_field(self):
        with self.assertLogs("wharf_sheet", level="WARNING") as captured:
            result = hardened_parse(b"SENTINEL-FIELD,1\r\n# bad\r\n")
        self.assertEqual(result.code, "E-comment")
        text = "\n".join(captured.output)
        self.assertIn("E-comment", text)
        self.assertIn("D-comments", text)
        self.assertNotIn("SENTINEL-FIELD", text)

    def test_repair_log_names_the_decision_and_omits_the_field(self):
        with self.assertLogs("wharf_sheet", level="INFO") as captured:
            result = legacy_parse(b"# remark\r\nx,y\r\n")
        self.assertEqual(result.records, (("x", "y"),))
        text = "\n".join(captured.output)
        self.assertIn("repair decision=D-comments", text)
        self.assertNotIn("remark", text)
