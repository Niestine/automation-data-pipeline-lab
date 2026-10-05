"""Maintenance events are also written to the contract_lab logger."""

from __future__ import annotations

import json
import logging
import unittest

import helpers  # noqa: F401

from contract_lab.client import ContractClient, ScriptedResponse
from helpers import GAUGE, NOW, bulletin, reading_schema

BODY = {"gauge_id": "g-1", "height_mm": 1200}


class LoggingTests(unittest.TestCase):
    def test_deprecation_warning_and_clock_fault_are_logged(self) -> None:
        current = bulletin(
            reading_schema(),
            deprecated=True,
            description="Deprecated bulletin. Replacement: /stations/{station_id}",
        )
        client = ContractClient(now=NOW)
        client.load(current)
        with self.assertLogs("contract_lab", level="INFO") as captured:
            client.call(
                "GET",
                GAUGE,
                ScriptedResponse(
                    status=200,
                    body=BODY,
                    headers={
                        "Deprecation": "@1719791999",
                        "Sunset": "Fri, 30 Jun 2023 23:59:59 GMT",
                    },
                ),
            )
        text = "\n".join(captured.output)
        self.assertIn("WARNING:contract_lab:deprecation_warning", text)
        self.assertIn("ERROR:contract_lab:clock_order_error", text)

    def test_records_reach_root_handlers_with_structured_fields(self) -> None:
        current = bulletin(reading_schema())
        client = ContractClient(now=NOW)
        client.load(current)
        with self.assertLogs(level="INFO") as captured:
            client.call(
                "GET",
                GAUGE,
                ScriptedResponse(status=200, body=BODY, headers={"Deprecation": "@1688169599"}),
            )
        record = captured.records[0]
        self.assertEqual(record.name, "contract_lab")
        self.assertEqual(record.levelno, logging.WARNING)
        self.assertEqual(record.event["kind"], "deprecation_warning")
        self.assertEqual(record.event["phase"], "already_deprecated")
        prefix = f"deprecation_warning GET {GAUGE} "
        message = record.getMessage()
        self.assertTrue(message.startswith(prefix), message)
        detail = json.loads(message[len(prefix):])
        self.assertEqual(detail["deprecation_at"], "2023-06-30T23:59:59Z")
        self.assertEqual(detail["channel"], "header")
        self.assertNotIn("replacement", detail)
