"""JSON contracts for orders, list pages, webhooks, and acks.

The lab never writes a payload that fails these checks. Python's json
module accepts NaN/Infinity; parse_json rejects those constants.
"""

from __future__ import annotations

from typing import Any
import json
import math
import re

from .errors import SchemaError
from .models import CURRENCIES, ORDER_STATUSES, WEBHOOK_TYPES, Order


ISO_Z = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"

ORDER_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sku", "qty", "unit_cents"],
    "properties": {
        "sku": {"type": "string", "pattern": r"^SKU-[0-9]{3,}$"},
        "qty": {"type": "integer", "minimum": 1, "maximum": 999},
        "unit_cents": {"type": "integer", "minimum": 0, "maximum": 100_000_000},
    },
}

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "status",
        "amount_cents",
        "currency",
        "updated_at",
        "version",
        "customer_ref",
        "items",
    ],
    "properties": {
        "id": {"type": "string", "pattern": r"^ORD-[0-9]{4,}$"},
        "status": {"type": "string", "enum": list(ORDER_STATUSES)},
        "amount_cents": {"type": "integer", "minimum": 0, "maximum": 100_000_000},
        "currency": {"type": "string", "enum": list(CURRENCIES)},
        "updated_at": {"type": "string", "pattern": ISO_Z},
        "version": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        "customer_ref": {"type": "string", "pattern": r"^CUST-[0-9]{3,}$"},
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": ORDER_ITEM_SCHEMA,
        },
    },
}

PAGE_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object", "items", "next_cursor", "has_more", "limit"],
    "properties": {
        "object": {"type": "string", "enum": ["list"]},
        "items": {"type": "array"},
        "next_cursor": {"type": ["string", "null"], "minLength": 1},
        "has_more": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
}

WEBHOOK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "type", "created_at", "data"],
    "properties": {
        "id": {"type": "string", "pattern": r"^evt_[0-9]{4,}$"},
        "type": {"type": "string", "enum": list(WEBHOOK_TYPES)},
        "created_at": {"type": "string", "pattern": ISO_Z},
        "data": {"type": "object"},
    },
}

ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["order_id", "version", "action"],
    "properties": {
        "order_id": {"type": "string", "pattern": r"^ORD-[0-9]{4,}$"},
        "version": {"type": "integer", "minimum": 1},
        "action": {"type": "string", "enum": ["ack_shipment"]},
    },
}


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _types_ok(expected: Any, value: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(item, value) for item in expected)
    return _type_ok(expected, value)


def validate_schema(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Return machine-readable errors; empty means the value matches."""
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not _types_ok(expected_type, value):
        got = type(value).__name__
        label = "|".join(expected_type) if isinstance(expected_type, list) else expected_type
        errors.append(f"{path}: expected {label}, got {got}")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not an allowed value")

    if isinstance(expected_type, list):
        numeric = any(item in {"number", "integer"} for item in expected_type)
    else:
        numeric = expected_type in {"number", "integer"}
    if numeric and isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            errors.append(f"{path}: {value} is not a finite number")
            return errors
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above maximum {schema['maximum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: length {len(value)} is below minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: length {len(value)} is above maxLength {schema['maxLength']}")
        pattern = schema.get("pattern")
        if pattern and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}: {value!r} does not match {pattern}")

    if expected_type == "array" or "items" in schema or "minItems" in schema:
        if not isinstance(value, list):
            if expected_type == "array":
                return errors
            errors.append(f"{path}: expected array")
            return errors
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: length {len(value)} is below minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: length {len(value)} is above maxItems {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item_schema, item, f"{path}[{index}]"))

    if expected_type == "object" or "properties" in schema:
        if not isinstance(value, dict):
            return errors
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: missing required field")
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                errors.extend(validate_schema(properties[key], item, f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}.{key}: additional property not allowed")
            elif isinstance(additional, dict):
                errors.extend(validate_schema(additional, item, f"{path}.{key}"))

    return errors


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name}")


def parse_json(raw: str | bytes) -> Any:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as exc:
        raise SchemaError("body is not utf-8", errors=[str(exc)]) from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid JSON: {exc}", errors=[str(exc)]) from exc
    except ValueError as exc:
        raise SchemaError(str(exc), errors=[str(exc)]) from exc


def dumps_canonical(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def validate_order_dict(data: Any) -> list[str]:
    errors = validate_schema(ORDER_SCHEMA, data)
    if errors:
        return errors
    total = sum(item["qty"] * item["unit_cents"] for item in data["items"])
    if total != data["amount_cents"]:
        errors.append(
            f"$.amount_cents: {data['amount_cents']} does not equal line-item total {total}"
        )
    return errors


def validate_page_envelope(data: Any) -> list[str]:
    errors = validate_schema(PAGE_ENVELOPE_SCHEMA, data)
    if errors:
        return errors
    has_more = data["has_more"]
    cursor = data["next_cursor"]
    if has_more and not isinstance(cursor, str):
        errors.append("$.next_cursor: required string when has_more is true")
    if not has_more and cursor is not None:
        errors.append("$.next_cursor: must be null when has_more is false")
    return errors


def validate_webhook_dict(data: Any) -> list[str]:
    errors = validate_schema(WEBHOOK_SCHEMA, data)
    if errors:
        return errors
    errors.extend(validate_order_dict(data["data"]))
    if errors:
        return errors
    event_type = data["type"]
    status = data["data"]["status"]
    if event_type == "order.cancelled" and status != "cancelled":
        errors.append("$.data.status: order.cancelled events must carry status cancelled")
    return errors


def validate_ack_dict(data: Any) -> list[str]:
    return validate_schema(ACK_SCHEMA, data)


def require_order(data: Any) -> Order:
    errors = validate_order_dict(data)
    if errors:
        raise SchemaError("order failed schema validation", errors=errors)
    return Order.from_validated(data)
