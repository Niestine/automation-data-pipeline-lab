"""Directional OpenAPI diff ids transcribed from the oasdiff rule extract.

``level`` is the Breaking or Info token that sits on the line above each id in
the archived rule list. Compliance scoring does not read this column. An Info
row must not become a non-breaking Serbout verdict, and a Breaking row must
not become a Serbout break, except where ``changes.impact_for`` states a
separate rule.
"""

from __future__ import annotations

# id, location, axis, level
_ROWS: tuple[tuple[str, str, str, str], ...] = (
    ("request-body-list-of-types-narrowed", "request", "list-of-types", "Breaking"),
    ("request-body-list-of-types-widened", "request", "list-of-types", "Info"),
    ("request-body-schema-became-false", "request", "schema-boolean", "Breaking"),
    ("request-body-schema-became-not-false", "request", "schema-boolean", "Info"),
    ("request-body-type-changed", "request", "type", "Breaking"),
    ("request-body-type-compatible", "request", "type", "Info"),
    ("request-body-type-generalized", "request", "type", "Info"),
    ("request-property-content-encoding-changed", "request", "content-encoding", "Breaking"),
    ("request-property-content-media-type-changed", "request", "content-media-type", "Breaking"),
    ("request-property-list-of-types-narrowed", "request", "list-of-types", "Breaking"),
    ("request-property-list-of-types-widened", "request", "list-of-types", "Info"),
    ("request-property-schema-became-false", "request", "schema-boolean", "Breaking"),
    ("request-property-schema-became-not-false", "request", "schema-boolean", "Info"),
    ("request-property-type-changed", "request", "type", "Breaking"),
    ("request-property-type-compatible", "request", "type", "Info"),
    ("request-property-type-generalized", "request", "type", "Info"),
    ("response-body-list-of-types-narrowed", "response", "list-of-types", "Info"),
    ("response-body-list-of-types-widened", "response", "list-of-types", "Breaking"),
    ("response-body-schema-became-false", "response", "schema-boolean", "Breaking"),
    ("response-body-schema-became-not-false", "response", "schema-boolean", "Info"),
    ("response-body-type-changed", "response", "type", "Breaking"),
    ("response-body-type-compatible", "response", "type", "Info"),
    ("response-body-type-generalized", "response", "type", "Breaking"),
    ("response-body-type-specialized", "response", "type", "Info"),
    ("response-property-content-encoding-changed", "response", "content-encoding", "Breaking"),
    ("response-property-content-media-type-changed", "response", "content-media-type", "Breaking"),
    ("response-optional-property-became-read-only", "response", "mutability", "Info"),
    ("response-optional-property-became-write-only", "response", "mutability", "Info"),
    ("response-required-property-became-not-read-only", "response", "mutability", "Info"),
    ("response-required-property-became-not-write-only", "response", "mutability", "Info"),
    ("response-required-property-became-read-only", "response", "mutability", "Info"),
    ("response-required-property-became-write-only", "response", "mutability", "Info"),
)

RULES: tuple[dict[str, str], ...] = tuple(
    {"id": rule_id, "location": location, "axis": axis, "level": level}
    for rule_id, location, axis, level in _ROWS
)

# The optional read-only id recovered from the archive, not the cut token.
OPTIONAL_RESPONSE_READ_ONLY = "response-optional-property-became-read-only"


def rule_by_id(rule_id: str) -> dict[str, str]:
    for row in RULES:
        if row["id"] == rule_id:
            return row
    raise KeyError(rule_id)
