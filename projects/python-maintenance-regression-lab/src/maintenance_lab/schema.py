"""JSON contracts for catalogs and v2 feeds.

Python's json module accepts NaN/Infinity; parse_json_text rejects those constants.
"""

from __future__ import annotations

from typing import Any
import json
import math
import re

from .errors import SchemaError
from .models import CURRENCIES, FEED_VERSIONS, MAX_IMAGE_URL_LENGTH, MAX_PRICE_MINOR, SKU_PATTERN
from .persist import fingerprint_fields


PRODUCT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "sku",
        "title",
        "price_cents",
        "currency",
        "stock",
        "active",
        "image_url",
        "version",
        "updated_at_ms",
        "source_version",
    ],
    "properties": {
        "sku": {"type": "string", "pattern": SKU_PATTERN},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "price_cents": {"type": "integer", "minimum": 0, "maximum": MAX_PRICE_MINOR},
        "currency": {"type": "string", "enum": list(CURRENCIES)},
        "stock": {"type": "integer", "minimum": 0, "maximum": 1_000_000_000},
        "active": {"type": "boolean"},
        "image_url": {"type": ["string", "null"], "maxLength": MAX_IMAGE_URL_LENGTH},
        "version": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        "updated_at_ms": {"type": "integer", "minimum": 0, "maximum": 4_102_444_800_000},
        "source_version": {"type": "string", "enum": list(FEED_VERSIONS)},
        "fingerprint": {"type": "string", "minLength": 64, "maxLength": 64},
    },
}

CATALOG_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["products"],
    "properties": {
        "products": {
            "type": "array",
            "maxItems": 10_000,
            "items": PRODUCT_SCHEMA,
        }
    },
}

V2_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["sku"],
    "properties": {
        "sku": {"type": "string", "minLength": 1, "maxLength": 64},
        "title": {"type": "string"},
        "product_name": {"type": "string"},
        "price": {"type": ["string", "integer"]},
        "price_cents": {"type": "integer", "minimum": 0},
        "currency": {"type": "string", "enum": list(CURRENCIES)},
        "stock": {"type": ["integer", "string", "null"]},
        "qty": {"type": ["integer", "string", "null"]},
        "active": {"type": ["boolean", "string"]},
        "image": {"type": ["string", "null"]},
        "image_url": {"type": ["string", "null"]},
        "updated_at": {"type": "string", "minLength": 1},
    },
}

V2_FEED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "version": {"type": "string", "enum": list(FEED_VERSIONS)},
        "currency": {"type": "string", "enum": list(CURRENCIES)},
        "items": {
            "type": "array",
            "maxItems": 10_000,
            "items": V2_ITEM_SCHEMA,
        },
    },
}


def _reject_constant(name: str) -> Any:
    raise SchemaError(f"non-finite JSON number {name}")


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid JSON: {exc}") from exc


def check_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not _matches_type(value, allowed):
            errors.append(f"{path}: expected {types}, got {type(value).__name__}")
            return errors
    if value is None:
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not an allowed value")
    if "pattern" in schema and isinstance(value, str):
        if re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match pattern")
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than minLength")
    if "maxLength" in schema and isinstance(value, str) and len(value) > schema["maxLength"]:
        errors.append(f"{path}: longer than maxLength")
    if "minimum" in schema and _is_number(value) and value < schema["minimum"]:
        errors.append(f"{path}: below minimum")
    if "maximum" in schema and _is_number(value) and value > schema["maximum"]:
        errors.append(f"{path}: above maximum")
    if isinstance(value, dict) and (
        schema.get("type") == "object" or "properties" in schema or "additionalProperties" in schema
    ):
        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        if additional is False:
            extra = sorted(set(value) - set(props))
            for key in extra:
                errors.append(f"{path}.{key}: additional property")
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}: required")
        for key, sub in props.items():
            if key in value:
                errors.extend(check_schema(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list) and schema.get("type") == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(check_schema(item, item_schema, f"{path}[{index}]"))
    return errors


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and not isinstance(value, bool)


def _matches_type(value: Any, allowed: list[str]) -> bool:
    for expected in allowed:
        if expected == "null" and value is None:
            return True
        if expected == "object" and isinstance(value, dict):
            return True
        if expected == "array" and isinstance(value, list):
            return True
        if expected == "string" and type(value) is str:
            return True
        if expected == "integer" and type(value) is int:
            return True
        if expected == "number" and _is_number(value):
            if isinstance(value, float) and not math.isfinite(value):
                return False
            return True
        if expected == "boolean" and type(value) is bool:
            return True
    return False


def require_schema(value: Any, schema: dict[str, Any], label: str) -> None:
    errors = check_schema(value, schema, "$")
    if errors:
        raise SchemaError(f"{label} is invalid: {errors[0]}", errors)


def product_fingerprint(data: dict[str, Any]) -> str:
    image = data.get("image_url")
    return fingerprint_fields(
        str(data["sku"]),
        str(data["title"]),
        int(data["price_cents"]),
        str(data["currency"]),
        int(data["stock"]),
        bool(data["active"]),
        None if image in (None, "") else str(image),
    )


def load_catalog_payload(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise SchemaError("catalog must be a JSON object")
    require_schema(data, CATALOG_FILE_SCHEMA, "catalog")
    products = data["products"]
    seen: set[str] = set()
    loaded: list[dict[str, Any]] = []
    for index, row in enumerate(products):
        sku = row["sku"]
        if sku in seen:
            raise SchemaError(f"duplicate sku {sku}")
        seen.add(sku)
        expected = product_fingerprint(row)
        stored = row.get("fingerprint")
        if stored is not None and stored != expected:
            raise SchemaError(f"catalog.products[{index}].fingerprint does not match body")
        copy = dict(row)
        copy["fingerprint"] = expected
        loaded.append(copy)
    return loaded
