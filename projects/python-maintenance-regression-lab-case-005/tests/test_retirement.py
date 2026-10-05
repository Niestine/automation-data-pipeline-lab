"""Deprecation marks, replacement text, and managed versus unmanaged removal."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from contract_lab.retirement import (
    REPLACEMENT_MARKER,
    assess,
    deprecated_marks,
    extract_replacement,
    index_document,
    protocol_ok,
)
from helpers import GAUGE, bulletin, document, flat_schema, reading_schema


class RetirementTests(unittest.TestCase):
    def test_replacement_marker_is_explicit(self) -> None:
        self.assertEqual(REPLACEMENT_MARKER, "Replacement:")
        self.assertEqual(
            extract_replacement("Deprecated bulletin. Replacement: /stations/{station_id}"),
            "/stations/{station_id}",
        )
        self.assertIsNone(extract_replacement("Use the station bulletin instead."))
        self.assertIsNone(extract_replacement("Replacement:"))

    def test_parameter_deprecation_impacts_the_operation(self) -> None:
        schema = flat_schema({"gauge_id": {"type": "string"}}, ["gauge_id"])
        plain = bulletin(schema)
        marked = bulletin(
            schema,
            parameters=[
                {
                    "name": "unit",
                    "in": "query",
                    "deprecated": True,
                    "description": "Replacement: unit_code",
                    "schema": {"type": "string"},
                }
            ],
        )
        report = assess([plain, marked])
        self.assertEqual(report.impacted_by_version[0], [])
        self.assertEqual(report.impacted_by_version[1], [f"GET {GAUGE}"])
        marks = deprecated_marks(marked, "GET", GAUGE)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0].kind, "parameter")
        self.assertEqual(marks[0].replacement, "unit_code")
        self.assertTrue(marks[0].pointer.endswith("/parameters/0"))

    def test_field_mark_inside_ref_then_managed_removal(self) -> None:
        first = bulletin(reading_schema())
        second = bulletin(
            reading_schema(
                {
                    "legacy_height": {
                        "type": "integer",
                        "deprecated": True,
                        "description": "Old field. Replacement: height_mm",
                    }
                }
            )
        )
        third = bulletin(reading_schema())
        report = assess([first, second, third])
        self.assertTrue(protocol_ok(report))
        fields = [item for item in report.removals if item.kind == "response_field"]
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].status, "managed")
        self.assertEqual(fields[0].name, "legacy_height")
        marks = deprecated_marks(second, "GET", GAUGE)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0].replacement, "height_mm")
        self.assertIn("/schema/$defs/core/properties/legacy_height", marks[0].pointer)

    def test_unmanaged_field_removal_fails_the_protocol(self) -> None:
        first = bulletin(
            reading_schema(
                {"legacy_height": {"type": "integer", "description": "Still current."}}
            )
        )
        second = bulletin(reading_schema())
        report = assess([first, second])
        self.assertFalse(protocol_ok(report))
        self.assertEqual(report.removals[0].status, "unmanaged")

    def test_operation_removal_covers_its_children(self) -> None:
        present = bulletin(
            flat_schema({"gauge_id": {"type": "string"}}, ["gauge_id"]),
            deprecated=True,
            description="Replacement: /stations/{station_id}",
        )
        gone = document({GAUGE: {}})
        report = assess([present, gone])
        self.assertTrue(protocol_ok(report))
        self.assertEqual(len(report.removals), 1)
        self.assertEqual(report.removals[0].kind, "operation")
        self.assertEqual(report.removals[0].status, "managed")

    def test_nested_fields_and_a_shared_ref_keep_their_own_identity(self) -> None:
        def schema(station_props):
            return {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "from_station": {"$ref": "#/$defs/station"},
                    "to_station": {"$ref": "#/$defs/station"},
                },
                "$defs": {"station": {"type": "object", "properties": station_props}},
            }

        both = {"name": {"type": "string"}, "code": {"type": "string"}}
        names = sorted(
            element.name
            for element in index_document(bulletin(schema(both))).values()
            if element.kind == "response_field"
        )
        self.assertEqual(
            names,
            [
                "from_station",
                "from_station.code",
                "from_station.name",
                "name",
                "to_station",
                "to_station.code",
                "to_station.name",
            ],
        )
        report = assess([bulletin(schema(both)), bulletin(schema({"code": {"type": "string"}}))])
        self.assertFalse(protocol_ok(report))
        self.assertEqual(
            sorted(item.name for item in report.removals),
            ["from_station.name", "to_station.name"],
        )
