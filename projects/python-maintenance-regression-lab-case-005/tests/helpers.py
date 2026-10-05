"""Path bootstrap and bulletin fixtures. Synthetic data only."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)
GAUGE = "/gauges/{gauge_id}"
URL_A = "https://gauge.example.test/gauges/g-1"
URL_B = "https://gauge.example.test/gauges/g-2"
SCHEMA_ID = "https://gauge.example.test/schemas/reading"


def document(paths, *, version="1.0.0", description="Synthetic harbor gauge bulletin.", observed_at=None, openapi="3.1.1"):
    body = {
        "openapi": openapi,
        "info": {
            "title": "Harbor Gauge Bulletin",
            "version": version,
            "description": description,
        },
        "paths": paths,
    }
    if observed_at is not None:
        body["x-observed-at"] = observed_at
    return body


def flat_schema(properties, required=None):
    body = {"type": "object", "properties": properties}
    if required is not None:
        body["required"] = list(required)
    return body


def reading_schema(properties=None, required=None):
    fields = {
        "gauge_id": {"type": "string"},
        "height_mm": {"type": "integer"},
    }
    if properties:
        fields.update(properties)
    return {
        "$id": SCHEMA_ID,
        "type": "object",
        "allOf": [{"$ref": "#/$defs/core"}],
        "$defs": {
            "core": {
                "type": "object",
                "properties": fields,
                "required": list(required or ["gauge_id", "height_mm"]),
            }
        },
    }


def operation(*, schema, parameters=None, deprecated=False, description="", sunset=None, request_schema=None, security=None, extra_responses=None):
    operation_body = {
        "operationId": "readGauge",
        "description": description,
        "responses": {
            "200": {
                "description": "A gauge reading",
                "content": {"application/json": {"schema": schema}},
            }
        },
    }
    if deprecated:
        operation_body["deprecated"] = True
    if parameters is not None:
        operation_body["parameters"] = parameters
    if sunset is not None:
        operation_body["x-sunset"] = sunset
    if request_schema is not None:
        operation_body["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": request_schema}},
        }
    if security is not None:
        operation_body["security"] = security
    if extra_responses:
        operation_body["responses"].update(extra_responses)
    return operation_body


def bulletin(schema, *, version="1.0.0", description="Synthetic harbor gauge bulletin.", observed_at=None, **operation_flags):
    return document(
        {GAUGE: {"get": operation(schema=schema, description=description, **operation_flags)}},
        version=version,
        description=description,
        observed_at=observed_at,
    )


def pair(before_schema, after_schema, *, side, before_flags=None, after_flags=None, observed_at=None):
    """Two documents that differ in one request or response schema."""

    stable = flat_schema({"gauge_id": {"type": "string"}})
    if side == "response":
        left = bulletin(before_schema, **(before_flags or {}))
        right = bulletin(after_schema, observed_at=observed_at, **(after_flags or {}))
    else:
        left = bulletin(stable, request_schema=before_schema, **(before_flags or {}))
        right = bulletin(stable, request_schema=after_schema, observed_at=observed_at, **(after_flags or {}))
    return left, right


def unix_seconds(year, month, day, hour=0, minute=0, second=0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp())
