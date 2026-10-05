"""Strict structured-output dialect plus optional format assertion.

Admission follows the archived OpenAI structured-outputs subset: a closed
root object, every property required, null unions for absence, and the
constraint keywords that guide lists (pattern, selected formats, numeric
bounds, minItems, maxItems). minLength and maxLength are rejected.

Instance checking follows JSON Schema draft 2020-12 for the keywords this
dialect keeps. format is an annotation unless format assertion is enabled,
and even then it is syntactic. A passing schema is not an authorization.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import SchemaAdmissionError

ALLOWED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
    }
)

FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?$")
_DURATION = re.compile(r"^P(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+S)?)?$")
_HOSTNAME = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_IPV6 = re.compile(r"^[0-9A-Fa-f:]+$")


def _type_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    raise SchemaAdmissionError("type must be a string or an array of strings")


def admit(schema: Any, path: str = "$") -> None:
    """Reject schemas outside the dialect. Raise SchemaAdmissionError."""
    if not isinstance(schema, dict):
        raise SchemaAdmissionError(f"{path} is not an object schema")
    for key in schema:
        if key in {"minLength", "maxLength"} or key not in ALLOWED_KEYWORDS:
            raise SchemaAdmissionError(f"{path} rejects keyword {key}")
    if "format" in schema and schema["format"] not in FORMATS:
        raise SchemaAdmissionError(f"{path} format is outside the supported set")
    types = _type_set(schema.get("type")) if "type" in schema else set()
    if path == "$":
        if types != {"object"}:
            raise SchemaAdmissionError("root schema must be type object")
        if schema.get("additionalProperties") is not False:
            raise SchemaAdmissionError("root additionalProperties must be false")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise SchemaAdmissionError(f"{path} properties must be an object")
        if schema.get("additionalProperties") is not False:
            raise SchemaAdmissionError(f"{path} additionalProperties must be false")
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(props):
            raise SchemaAdmissionError(f"{path} required must list every property")
        for name, sub in props.items():
            admit(sub, f"{path}.{name}")
    if "items" in schema:
        admit(schema["items"], f"{path}.items")
    if "anyOf" in schema:
        if not isinstance(schema["anyOf"], list) or not schema["anyOf"]:
            raise SchemaAdmissionError(f"{path} anyOf must be a non-empty array")
        for index, sub in enumerate(schema["anyOf"]):
            admit(sub, f"{path}.anyOf[{index}]")
    if "minimum" in schema or "maximum" in schema or "multipleOf" in schema:
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            if key in schema and not isinstance(schema[key], (int, float)):
                raise SchemaAdmissionError(f"{path} {key} must be numeric")
    if "minItems" in schema or "maxItems" in schema:
        for key in ("minItems", "maxItems"):
            if key in schema and not isinstance(schema[key], int):
                raise SchemaAdmissionError(f"{path} {key} must be an integer")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _matches_type(value: Any, types: set[str]) -> bool:
    if not types:
        return True
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": _is_number,
        "null": lambda item: item is None,
    }
    return any(checks[name](value) for name in types if name in checks)


def _ipv4_ok(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        number = int(part)
        if number > 255 or (len(part) > 1 and part.startswith("0")):
            return False
    return True


def format_ok(fmt: str, value: str) -> bool:
    """Syntactic format check. This does not prove a mailbox or host exists."""
    if fmt == "email":
        return _EMAIL.fullmatch(value) is not None
    if fmt == "date":
        return _DATE.fullmatch(value) is not None
    if fmt == "time":
        return _TIME.fullmatch(value) is not None
    if fmt == "date-time":
        return _DATE_TIME.fullmatch(value) is not None
    if fmt == "duration":
        if value in {"", "P", "PT"} or _DURATION.fullmatch(value) is None:
            return False
        return any(character.isdigit() for character in value)
    if fmt == "hostname":
        return _HOSTNAME.fullmatch(value) is not None
    if fmt == "ipv4":
        return _ipv4_ok(value)
    if fmt == "ipv6":
        return _IPV6.fullmatch(value) is not None and value.count(":") >= 2
    if fmt == "uuid":
        return _UUID.fullmatch(value) is not None
    return False


def validate_instance(schema: dict[str, Any], instance: Any, *, format_assertion: bool, path: str = "$") -> list[str]:
    """Return structural error strings. An empty list is a structural pass."""
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            if not validate_instance(branch, instance, format_assertion=format_assertion, path=path):
                return []
        return [f"{path} anyOf"]
    errors: list[str] = []
    types = _type_set(schema.get("type")) if "type" in schema else set()
    if types and not _matches_type(instance, types):
        return [f"{path} type"]
    if "const" in schema and instance != schema["const"]:
        return [f"{path} const"]
    if "enum" in schema and instance not in schema["enum"]:
        return [f"{path} enum"]
    if isinstance(instance, str) and "pattern" in schema:
        if re.fullmatch(schema["pattern"], instance) is None:
            errors.append(f"{path} pattern")
    if format_assertion and isinstance(instance, str) and "format" in schema:
        if not format_ok(schema["format"], instance):
            errors.append(f"{path} format")
    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path} minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path} maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{path} exclusiveMinimum")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{path} exclusiveMaximum")
        if "multipleOf" in schema:
            divisor = schema["multipleOf"]
            if divisor == 0 or (instance / divisor) % 1 != 0:
                errors.append(f"{path} multipleOf")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path} minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path} maxItems")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(
                    validate_instance(
                        schema["items"], item, format_assertion=format_assertion, path=f"{path}[{index}]"
                    )
                )
    if isinstance(instance, dict) and "properties" in schema:
        props = schema["properties"]
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errors.append(f"{path} additionalProperties {key}")
        for key in schema.get("required") or []:
            if key not in instance:
                errors.append(f"{path} required {key}")
        for key, sub in props.items():
            if key in instance:
                errors.extend(
                    validate_instance(sub, instance[key], format_assertion=format_assertion, path=f"{path}.{key}")
                )
    return errors
