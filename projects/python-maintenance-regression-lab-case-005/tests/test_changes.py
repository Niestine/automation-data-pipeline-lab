"""Structural diffs are classified before the version string is read."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from contract_lab.changes import diff_openapi, impact_for
from contract_lab.compliance import evaluate_release
from helpers import GAUGE, bulletin, document, flat_schema, operation, unix_seconds

SUNSET = "Sun, 30 Jun 2024 23:59:59 UTC"


def _flat(properties, required=None):
    return flat_schema(properties, required)


class ChangeTests(unittest.TestCase):
    def test_added_path_and_operation_are_non_breaking_for_both_readers(self) -> None:
        base = bulletin(_flat({"gauge_id": {"type": "string"}}))
        extra_path = document(
            {
                GAUGE: base["paths"][GAUGE],
                "/stations/{station_id}": {
                    "get": operation(schema=_flat({"station_id": {"type": "string"}}))
                },
            }
        )
        self.assertEqual(diff_openapi(base, extra_path), ["path_added"])
        with_post = document(
            {
                GAUGE: {
                    "get": base["paths"][GAUGE]["get"],
                    "post": operation(schema=_flat({"gauge_id": {"type": "string"}})),
                }
            }
        )
        self.assertEqual(diff_openapi(base, with_post), ["operation_added"])
        for change_id in ("path_added", "operation_added"):
            self.assertEqual(impact_for(change_id, "strict"), "non_breaking")
            self.assertEqual(impact_for(change_id, "tolerant"), "non_breaking")

    def test_response_property_addition_is_reader_conditional(self) -> None:
        before = bulletin(_flat({"gauge_id": {"type": "string"}, "height_mm": {"type": "integer"}}, ["gauge_id"]))
        after = bulletin(
            _flat(
                {
                    "gauge_id": {"type": "string"},
                    "height_mm": {"type": "integer"},
                    "spare_note": {"type": "string"},
                },
                ["gauge_id"],
            )
        )
        self.assertEqual(diff_openapi(before, after), ["response_property_added"])
        self.assertEqual(impact_for("response_property_added", "strict"), "breaking")
        self.assertEqual(impact_for("response_property_added", "tolerant"), "non_breaking")

    def test_delete_request_parameter_and_required_element(self) -> None:
        before = bulletin(_flat({"gauge_id": {"type": "string"}}))
        added = bulletin(
            _flat({"gauge_id": {"type": "string"}}),
            parameters=[{"name": "unit", "in": "query", "schema": {"type": "string"}}],
        )
        self.assertEqual(diff_openapi(before, added), ["request_parameter_added"])
        self.assertEqual(impact_for("request_parameter_added", "strict"), "breaking")
        optional = bulletin(_flat({"height_mm": {"type": "integer"}}, []))
        required = bulletin(_flat({"height_mm": {"type": "integer"}}, ["height_mm"]))
        self.assertEqual(diff_openapi(optional, required), ["required_element_added"])
        deleted = bulletin(_flat({"gauge_id": {"type": "string"}}, ["gauge_id"]))
        full = bulletin(
            _flat(
                {"gauge_id": {"type": "string"}, "height_mm": {"type": "integer"}},
                ["gauge_id"],
            )
        )
        self.assertEqual(diff_openapi(full, deleted), ["response_property_deleted"])

    def test_removal_ids_depend_on_notice_and_sunset_and_not_on_the_version(self) -> None:
        schema = _flat({"gauge_id": {"type": "string"}})
        present = bulletin(schema)
        gone = document({GAUGE: {}})
        self.assertEqual(diff_openapi(present, gone), ["operation_removed_no_notice"])
        deprecated = bulletin(schema, deprecated=True, description="Replacement: /stations/{station_id}")
        self.assertEqual(diff_openapi(deprecated, gone), ["operation_removed_after_deprecation"])
        announced = bulletin(schema, sunset=SUNSET)
        before_sunset = document({GAUGE: {}}, observed_at=unix_seconds(2024, 1, 1))
        after_sunset = document({GAUGE: {}}, observed_at=unix_seconds(2024, 7, 1))
        at_sunset = document({GAUGE: {}}, observed_at=unix_seconds(2024, 6, 30, 23, 59, 59))
        self.assertEqual(diff_openapi(announced, before_sunset), ["operation_removed_before_sunset"])
        self.assertEqual(diff_openapi(announced, after_sunset), ["operation_removed_after_sunset"])
        self.assertEqual(diff_openapi(announced, at_sunset), ["operation_removed_after_sunset"])
        self.assertEqual(impact_for("operation_removed_before_sunset", "tolerant"), "breaking")
        self.assertEqual(impact_for("operation_removed_after_sunset", "strict"), "undecidable")
        path_gone = document({})
        self.assertEqual(diff_openapi(present, path_gone), ["path_removed_no_notice"])

    def test_type_to_enum_and_nullable_are_their_own_ids(self) -> None:
        plain = bulletin(_flat({"status": {"type": "string"}}))
        enumerated = bulletin(_flat({"status": {"type": "string", "enum": ["ok", "hold"]}}))
        self.assertEqual(diff_openapi(plain, enumerated), ["type_changed_to_enum"])
        self.assertEqual(impact_for("type_changed_to_enum", "strict"), "breaking")
        self.assertEqual(impact_for("type_changed_to_enum", "tolerant"), "undecidable")
        closed = bulletin(_flat({"height_mm": {"type": "integer", "nullable": False}}))
        opened = bulletin(_flat({"height_mm": {"type": "integer", "nullable": True}}))
        self.assertEqual(diff_openapi(closed, opened), ["nullable_flag_changed"])
        self.assertEqual(impact_for("nullable_flag_changed", "strict"), "undecidable")

    def test_info_openapi_patch_and_description_edits_emit_nothing(self) -> None:
        left = bulletin(_flat({"gauge_id": {"type": "string"}}), version="1.2.0", description="One")
        right = bulletin(_flat({"gauge_id": {"type": "string"}}), version="2.0.0", description="Two")
        right["openapi"] = "3.1.0"
        self.assertEqual(diff_openapi(left, right), [])

    def test_change_ids_survive_a_rejected_version(self) -> None:
        before = bulletin(_flat({"gauge_id": {"type": "string"}, "height_mm": {"type": "integer"}}, ["gauge_id"]))
        after = bulletin(_flat({"gauge_id": {"type": "string"}}, ["gauge_id"]))
        evaluated = evaluate_release(before, after, "1.2.0", "20240101")
        self.assertEqual(evaluated["change_ids"], ["response_property_deleted"])
        self.assertIsNone(evaluated["version_kind"])
        self.assertIsNotNone(evaluated["version_error"])

    def test_response_body_type_compatible_is_the_tolerant_exception(self) -> None:
        self.assertEqual(impact_for("response-body-type-compatible", "strict"), "breaking")
        self.assertEqual(impact_for("response-body-type-compatible", "tolerant"), "non_breaking")
        self.assertEqual(impact_for("request-body-type-compatible", "tolerant"), "undecidable")
        self.assertEqual(impact_for("response-body-type-changed", "strict"), "breaking")
        self.assertEqual(impact_for("response-body-type-changed", "tolerant"), "undecidable")

    def test_dropping_a_type_list_is_generalized_like_dropping_one_type(self) -> None:
        listed = bulletin({"type": ["object", "null"]})
        open_body = bulletin({})
        self.assertEqual(diff_openapi(listed, open_body), ["response-body-type-generalized"])
        self.assertEqual(diff_openapi(open_body, listed), ["response-body-type-specialized"])
