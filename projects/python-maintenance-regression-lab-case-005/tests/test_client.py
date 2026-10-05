"""Scripted calls: warnings, clocks, readers, and removal."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import helpers  # noqa: F401

from contract_lab.client import ContractClient, ScriptedResponse
from contract_lab.errors import (
    HeaderGrammarError,
    OperationNotFound,
    OperationRemoved,
    SchemaRejected,
    UnmanagedRemoval,
)
from helpers import GAUGE, NOW, URL_A, URL_B, bulletin, document, flat_schema, reading_schema

BODY = {"gauge_id": "g-1", "height_mm": 1200}
RFC_SUNSET = "Sat, 31 Dec 2018 23:59:59 GMT"


def _client(document_body, history=None, **kwargs):
    client = ContractClient(now=kwargs.pop("now", NOW), **kwargs)
    client.load(document_body, history)
    return client


class ClientTests(unittest.TestCase):
    def test_description_channel_warns_without_a_header_and_keeps_the_object(self) -> None:
        schema = reading_schema()
        current = bulletin(
            schema,
            deprecated=True,
            description="Deprecated bulletin. Replacement: /stations/{station_id}",
        )
        result = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(status=200, body=BODY, url=URL_A),
        )
        self.assertEqual(result.business, BODY)
        self.assertEqual(result.phase, "not_deprecated")
        self.assertTrue(result.stability_assured)
        warnings = [event for event in result.events if event.kind == "deprecation_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].channel, "description")
        self.assertEqual(warnings[0].replacement, "/stations/{station_id}")
        self.assertIsNone(warnings[0].deprecation_at)
        self.assertIsNone(warnings[0].phase)
        self.assertTrue(warnings[0].element_pointer.startswith("#/paths/"))

    def test_header_channel_warns_without_replacement_text(self) -> None:
        current = bulletin(reading_schema(), description="Current bulletin.")
        result = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(
                status=200,
                body=BODY,
                url=URL_A,
                headers={"Deprecation": "@1735689600"},
            ),
        )
        self.assertEqual(result.business, BODY)
        self.assertEqual(result.phase, "scheduled")
        self.assertTrue(result.stability_assured)
        warnings = [event for event in result.events if event.kind == "deprecation_warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].channel, "header")
        self.assertIsNone(warnings[0].replacement)
        self.assertEqual(warnings[0].scope_uri, URL_A)

    def test_future_and_past_deprecation_do_not_change_the_business_object(self) -> None:
        current = bulletin(reading_schema())
        plain = _client(current).call("GET", GAUGE, ScriptedResponse(status=200, body=BODY))
        future = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(status=200, body=BODY, headers={"Deprecation": "@1735689600"}),
        )
        past = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(status=200, body=BODY, headers={"Deprecation": "@1688169599"}),
        )
        self.assertEqual(plain.business, future.business)
        self.assertEqual(plain.business, past.business)
        self.assertEqual(past.phase, "already_deprecated")
        self.assertFalse(past.stability_assured)
        self.assertEqual(future.phase, "scheduled")

    def test_policy_link_does_not_deprecate_or_fetch(self) -> None:
        current = bulletin(reading_schema())
        result = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(
                status=200,
                body=BODY,
                headers={
                    "Link": '<https://developer.example.test/deprecation>; rel="deprecation"; type="text/html"'
                },
            ),
        )
        self.assertEqual(result.business, BODY)
        self.assertEqual(result.phase, "not_deprecated")
        self.assertEqual(
            [event.kind for event in result.events],
            ["policy_link"],
        )
        self.assertEqual(result.events[0].target, "https://developer.example.test/deprecation")

    def test_reversed_clocks_stay_unswapped(self) -> None:
        current = bulletin(reading_schema())
        result = _client(current).call(
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
        clocks = [event for event in result.events if event.kind == "clock_order_error"]
        warnings = [event for event in result.events if event.kind == "deprecation_warning"]
        self.assertEqual(len(clocks), 1)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(clocks[0].deprecation_at, "2024-06-30T23:59:59Z")
        self.assertEqual(clocks[0].sunset_at, "2023-06-30T23:59:59Z")
        self.assertEqual(result.business, BODY)

    def test_equal_clocks_do_not_emit_the_fault(self) -> None:
        current = bulletin(reading_schema())
        result = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(
                status=200,
                body=BODY,
                headers={
                    "Deprecation": "@1688169599",
                    "Sunset": "Fri, 30 Jun 2023 23:59:59 GMT",
                },
            ),
        )
        self.assertFalse(any(event.kind == "clock_order_error" for event in result.events))
        self.assertEqual(result.business, BODY)

    def test_past_sunset_on_200_records_status_and_does_not_raise(self) -> None:
        current = bulletin(reading_schema())
        result = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(status=200, body=BODY, headers={"Sunset": RFC_SUNSET}),
        )
        elapsed = [event for event in result.events if event.kind == "sunset_elapsed"]
        self.assertEqual(len(elapsed), 1)
        self.assertEqual(elapsed[0].status, 200)
        self.assertFalse(any(event.kind == "eol_transition" for event in result.events))
        self.assertEqual(result.business, BODY)
        self.assertFalse(result.stability_assured)

    def test_410_at_sunset_is_eol_and_a_transport_error_is_not(self) -> None:
        current = bulletin(reading_schema())
        gone = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(status=410, body={"error": "gone"}, headers={"Sunset": RFC_SUNSET}),
        )
        self.assertEqual(
            [event.kind for event in gone.events],
            ["sunset_elapsed", "eol_transition"],
        )
        self.assertEqual(gone.events[1].status, 410)
        self.assertEqual(gone.events[1].detail, "status_410")
        self.assertIsNone(gone.business)
        broken = _client(current).call(
            "GET",
            GAUGE,
            ScriptedResponse(
                status=410,
                transport_error="connection_refused",
                headers={"Sunset": RFC_SUNSET},
            ),
        )
        self.assertEqual([event.kind for event in broken.events], ["sunset_elapsed"])
        self.assertIsNone(broken.events[0].status)
        self.assertEqual(broken.events[0].transport_error, "connection_refused")
        self.assertIsNone(broken.status)

    def test_base_switch_is_an_end_of_life_transition(self) -> None:
        current = bulletin(reading_schema())
        client = _client(current, replacement_base="https://next.example.test")
        result = client.call(
            "GET",
            GAUGE,
            ScriptedResponse(
                status=200,
                body=BODY,
                headers={"Sunset": RFC_SUNSET},
                used_base="https://next.example.test",
            ),
        )
        kinds = [event.kind for event in result.events]
        self.assertEqual(kinds, ["sunset_elapsed", "eol_transition"])
        self.assertEqual(result.events[1].detail, "base_switch")
        self.assertEqual(result.business, BODY)

    def test_sibling_uri_inherits_only_through_the_policy_map(self) -> None:
        current = bulletin(reading_schema())
        alone = _client(current)
        alone.call(
            "GET",
            GAUGE,
            ScriptedResponse(status=200, body=BODY, url=URL_A, headers={"Deprecation": "@1735689600"}),
        )
        sibling = alone.call("GET", GAUGE, ScriptedResponse(status=200, body=BODY, url=URL_B))
        self.assertEqual(sibling.events, [])
        mapped = _client(current, policy_map={URL_A: [URL_B]})
        mapped.call(
            "GET",
            GAUGE,
            ScriptedResponse(status=200, body=BODY, url=URL_A, headers={"Deprecation": "@1735689600"}),
        )
        inherited = mapped.call("GET", GAUGE, ScriptedResponse(status=200, body=BODY, url=URL_B))
        self.assertEqual(len(inherited.events), 1)
        self.assertTrue(inherited.events[0].inherited)
        self.assertEqual(inherited.events[0].scope_uri, URL_A)
        self.assertEqual(inherited.events[0].applied_uri, URL_B)
        self.assertEqual(inherited.business, BODY)

    def test_strict_reader_rejects_the_sibling_and_tolerant_projects_it_away(self) -> None:
        current = bulletin(reading_schema())
        payload = {**BODY, "spare_flag": True}
        tolerant = _client(current, reader="tolerant").call(
            "GET", GAUGE, ScriptedResponse(status=200, body=payload)
        )
        self.assertEqual(tolerant.business, BODY)
        self.assertTrue(tolerant.reader_results["strict"]["valid"] is False)
        self.assertTrue(tolerant.reader_results["tolerant"]["valid"] is True)
        strict = _client(current, reader="strict")
        with self.assertRaises(SchemaRejected) as raised:
            strict.call("GET", GAUGE, ScriptedResponse(status=200, body=payload))
        self.assertEqual(raised.exception.node["instanceLocation"], "/spare_flag")
        self.assertTrue(raised.exception.reader_results["tolerant"]["valid"])
        self.assertFalse(raised.exception.reader_results["strict"]["valid"])

    def test_boolean_height_fails_the_active_reader(self) -> None:
        current = bulletin(reading_schema())
        with self.assertRaises(SchemaRejected) as raised:
            _client(current).call(
                "GET",
                GAUGE,
                ScriptedResponse(status=200, body={"gauge_id": "g-1", "height_mm": True}),
            )
        self.assertEqual(raised.exception.node["instanceLocation"], "/height_mm")

    def test_illegal_header_raises_and_sunset_grammar_is_not_accepted_as_deprecation(self) -> None:
        current = bulletin(reading_schema())
        with self.assertRaises(HeaderGrammarError):
            _client(current).call(
                "GET",
                GAUGE,
                ScriptedResponse(status=200, body=BODY, headers={"Deprecation": RFC_SUNSET}),
            )

    def test_unmanaged_operation_removal_raises_and_managed_removal_is_distinct(self) -> None:
        schema = flat_schema({"gauge_id": {"type": "string"}}, ["gauge_id"])
        present = bulletin(schema)
        gone = document({GAUGE: {}})
        unmanaged = _client(gone, [present, gone])
        with self.assertRaises(UnmanagedRemoval):
            unmanaged.call("GET", GAUGE, ScriptedResponse(status=200, body={"gauge_id": "g-1"}))
        marked = bulletin(schema, deprecated=True, description="Replacement: /stations/{station_id}")
        managed = _client(gone, [marked, gone])
        with self.assertRaises(OperationRemoved):
            managed.call("GET", GAUGE, ScriptedResponse(status=200, body={"gauge_id": "g-1"}))
        missing = _client(present)
        with self.assertRaises(OperationNotFound):
            missing.call("GET", "/missing", ScriptedResponse(status=200, body={}))

    def test_middle_call_of_a_managed_history_matches_the_fixture_object(self) -> None:
        schema = reading_schema()
        first = bulletin(schema)
        second = bulletin(
            schema,
            deprecated=True,
            description="Deprecated bulletin. Replacement: /stations/{station_id}",
        )
        third = document({})
        client = _client(second, [first, second, third])
        result = client.call("GET", GAUGE, ScriptedResponse(status=200, body=BODY))
        self.assertEqual(result.business, BODY)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].channel, "description")

    def test_repeated_calls_are_identical(self) -> None:
        current = bulletin(reading_schema())
        client = _client(current)
        script = ScriptedResponse(status=200, body=BODY, headers={"Deprecation": "@1688169599"})
        first = client.call("GET", GAUGE, script)
        second = client.call("GET", GAUGE, script)
        self.assertEqual(first.business, second.business)
        self.assertEqual([event.as_dict() for event in first.events], [event.as_dict() for event in second.events])

    def test_sources_do_not_open_a_network_client(self) -> None:
        root = helpers.ROOT / "src" / "contract_lab"
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("urlopen", text)
            self.assertNotIn("import socket", text)
            self.assertNotIn("import urllib", text)

    def test_future_sunset_410_is_not_an_end_of_life_record(self) -> None:
        current = bulletin(reading_schema())
        result = _client(current, now=datetime(2018, 1, 1, tzinfo=timezone.utc)).call(
            "GET",
            GAUGE,
            ScriptedResponse(status=410, headers={"Sunset": RFC_SUNSET}),
        )
        self.assertEqual(result.events, [])
        self.assertEqual(result.status, 410)

    def test_redirect_after_sunset_is_recorded_and_not_read_as_the_200_body(self) -> None:
        current = bulletin(reading_schema())
        for reader in ("strict", "tolerant"):
            result = _client(current, reader=reader).call(
                "GET",
                GAUGE,
                ScriptedResponse(status=301, headers={"Sunset": RFC_SUNSET, "Location": "/v2/gauges/g-1"}),
            )
            self.assertEqual([event.kind for event in result.events], ["sunset_elapsed"])
            self.assertEqual(result.events[0].status, 301)
            self.assertEqual(result.reader_results, {})
            self.assertIsNone(result.business)

    def test_a_documented_non_200_schema_is_the_one_validated(self) -> None:
        accepted = flat_schema({"job_id": {"type": "string"}}, ["job_id"])
        current = bulletin(
            reading_schema(),
            extra_responses={
                "202": {"description": "Queued", "content": {"application/json": {"schema": accepted}}}
            },
        )
        result = _client(current).call("GET", GAUGE, ScriptedResponse(status=202, body={"job_id": "j-1", "spare": 1}))
        self.assertEqual(result.business, {"job_id": "j-1"})
        self.assertFalse(result.reader_results["strict"]["valid"])
