"""Draft 2020-12 closed-object validator for cited_answer_v1.

`format` is not enforced. In the 2020-12 meta-schema the format vocabulary
is not an assertion. `end >= start` is checked in code because this
vocabulary has no cross-field numeric comparison.
"""

from __future__ import annotations

from typing import Any

DIALECT = "https://json-schema.org/draft/2020-12/schema"

SUPPORT_ENUM = ["supported", "partial", "unsupported", "contradicted", "abstain"]
KIND_ENUM = ["fact", "inference", "insufficient_evidence"]
VERDICT_ENUM = ["yes", "no"]

_STATEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "explanation", "verdict", "chunk_id"],
    "minProperties": 4,
    "maxProperties": 4,
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "explanation": {"type": "string", "minLength": 1},
        "verdict": {"enum": VERDICT_ENUM},
        "chunk_id": {"type": ["string", "null"], "minLength": 1},
    },
}

# Object keywords apply only to objects, so null passes this closed subschema.
_FALLBACK_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "required": ["statements"],
    "minProperties": 1,
    "maxProperties": 1,
    "properties": {
        "statements": {"type": "array", "minItems": 1, "items": _STATEMENT_SCHEMA},
    },
}

_CITATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chunk_id", "start", "end"],
    "minProperties": 3,
    "maxProperties": 3,
    "properties": {
        "chunk_id": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 0},
    },
}

_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claim_id", "text", "kind", "support", "citations", "fallback"],
    "minProperties": 6,
    "maxProperties": 6,
    "properties": {
        "claim_id": {"type": ["string", "null"], "minLength": 1},
        "text": {"type": "string", "minLength": 1},
        "kind": {"enum": KIND_ENUM},
        "support": {"enum": SUPPORT_ENUM},
        "citations": {"type": "array", "items": _CITATION_SCHEMA},
        "fallback": _FALLBACK_SCHEMA,
    },
}

RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": DIALECT,
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "item_id", "surface_text", "reverse_questions", "claims"],
    "minProperties": 5,
    "maxProperties": 5,
    "properties": {
        "schema_version": {"const": "cited_answer_v1"},
        "item_id": {"type": "string", "minLength": 1},
        "surface_text": {"type": "string", "minLength": 1},
        "reverse_questions": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "claims": {"type": "array", "minItems": 1, "items": _CLAIM_SCHEMA},
    },
}


def _type_ok(instance: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "null":
        return instance is None
    return False


def validate(instance: Any, schema: dict[str, Any] | bool, path: str = "$") -> list[str]:
    """Return validation errors. An empty list means the instance is valid."""
    if schema is False:
        return [f"{path}: schema false"]
    if schema is True or not isinstance(schema, dict):
        return []
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, item) for item in options):
            return [f"{path}: type {expected}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: const")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: enum")
    if isinstance(instance, str) and "minLength" in schema and len(instance) < schema["minLength"]:
        errors.append(f"{path}: minLength")
    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: maximum")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: minItems")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{index}]"))
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list):
            for index, item_schema in enumerate(prefix):
                if index >= len(instance):
                    break
                errors.extend(validate(instance[index], item_schema, f"{path}[{index}]"))
    if isinstance(instance, dict):
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}.{key}: additionalProperties")
        required = schema.get("required") or []
        for key in required:
            if key not in instance:
                errors.append(f"{path}.{key}: required")
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            errors.append(f"{path}: minProperties")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append(f"{path}: maxProperties")
        for key, sub in properties.items():
            if key in instance:
                errors.extend(validate(instance[key], sub, f"{path}.{key}"))
    return errors


def validate_response(instance: Any) -> list[str]:
    return validate(instance, RESPONSE_SCHEMA)


def citation_bound_errors(payload: dict[str, Any], chunk_lengths: dict[str, int]) -> list[str]:
    """Enforce end >= start and offsets inside the cited chunk. Not a schema keyword."""
    errors: list[str] = []
    for index, claim in enumerate(payload.get("claims") or []):
        if not isinstance(claim, dict):
            continue
        for cite_index, cite in enumerate(claim.get("citations") or []):
            if not isinstance(cite, dict):
                continue
            path = f"$.claims[{index}].citations[{cite_index}]"
            start = cite.get("start")
            end = cite.get("end")
            chunk_id = cite.get("chunk_id")
            if not isinstance(start, int) or isinstance(start, bool):
                continue
            if not isinstance(end, int) or isinstance(end, bool):
                continue
            if end < start:
                errors.append(f"{path}: end < start")
            if not isinstance(chunk_id, str) or chunk_id not in chunk_lengths:
                errors.append(f"{path}: unknown chunk_id")
                continue
            if end > chunk_lengths[chunk_id]:
                errors.append(f"{path}: end past chunk")
        if claim.get("claim_id") is not None and claim.get("fallback") is not None:
            errors.append(f"$.claims[{index}]: planted claim must have null fallback")
        if claim.get("claim_id") is None and claim.get("kind") != "insufficient_evidence":
            if not isinstance(claim.get("fallback"), dict):
                errors.append(f"$.claims[{index}]: unplanted claim requires fallback")
    return errors
