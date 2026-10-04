"""Structured-output contracts and a small JSON-schema validator.

The lab never trusts free-form model text. Completions are extracted,
parsed, and checked against agent_output_v1 before any tool runs.
"""

from __future__ import annotations

from typing import Any
import json
import math

from .models import ACTIONS, INTENTS, SCHEMA_NAME, AgentOutput


class ContractError(ValueError):
    def __init__(self, code: str, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.errors = list(errors or [])
        self.message = message


AGENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent",
        "confidence",
        "entities",
        "proposed_action",
        "action_args",
        "rationale",
        "needs_human",
    ],
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "entities": {"type": "object"},
        "proposed_action": {"type": "string", "enum": list(ACTIONS)},
        "action_args": {"type": "object"},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 500},
        "needs_human": {"type": "boolean"},
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


def validate_schema(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Return a list of machine-readable validation errors (empty if valid)."""
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not _type_ok(expected_type, value):
        errors.append(f"{path}: expected {expected_type}, got {type(value).__name__}")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not an allowed value")

    if expected_type in {"number", "integer"}:
        if not math.isfinite(value):
            errors.append(f"{path}: {value} is not a finite number")
            return errors
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above maximum {schema['maximum']}")

    if expected_type == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: length {len(value)} is below minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: length {len(value)} is above maxLength {schema['maxLength']}")

    if expected_type == "array" or "items" in schema:
        if not isinstance(value, list):
            errors.append(f"{path}: expected array")
            return errors
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item_schema, item, f"{path}[{index}]"))

    is_object_schema = expected_type == "object" or "properties" in schema or "required" in schema
    if is_object_schema:
        if not isinstance(value, dict):
            errors.append(f"{path}: expected object")
            return errors
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required property")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if key in properties:
                errors.extend(validate_schema(properties[key], child, f"{path}.{key}"))
            elif additional is False:
                errors.append(f"{path}.{key}: additional property not allowed")
            elif isinstance(additional, dict):
                errors.extend(validate_schema(additional, child, f"{path}.{key}"))
    return errors


def extract_json_object(text: str) -> str:
    if text is None or not str(text).strip():
        raise ContractError("parse_error", "empty model response")
    blob = str(text).strip()
    if blob.startswith("```"):
        lines = blob.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        blob = "\n".join(lines).strip()
    start = blob.find("{")
    end = blob.rfind("}")
    if start < 0 or end < 0 or end < start:
        raise ContractError("parse_error", "no JSON object found in model response")
    return blob[start : end + 1]


def _reject_constant(name: str) -> Any:
    raise ContractError("parse_error", f"non-standard JSON constant {name}")


def parse_agent_output(text: str) -> AgentOutput:
    raw = extract_json_object(text)
    try:
        data = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ContractError("parse_error", f"invalid JSON: {exc.msg}") from exc

    errors = validate_schema(AGENT_OUTPUT_SCHEMA, data)
    if errors:
        raise ContractError("schema_error", "response failed agent_output_v1", errors)

    return AgentOutput(
        intent=data["intent"],
        confidence=float(data["confidence"]),
        entities=dict(data["entities"]),
        proposed_action=data["proposed_action"],
        action_args=dict(data["action_args"]),
        rationale=data["rationale"],
        needs_human=data["needs_human"],
    )


def system_prompt() -> str:
    return (
        "You are a provider-neutral internal operations agent.\n"
        f"Return ONLY a JSON object that matches {SCHEMA_NAME}.\n"
        f"intent: {' | '.join(INTENTS)}\n"
        f"proposed_action: {' | '.join(ACTIONS)}\n"
        "confidence is a number between 0 and 1.\n"
        "entities and action_args are objects.\n"
        "rationale is a short string.\n"
        "needs_human is a boolean.\n"
        "Do not invent credentials. Prefer refuse when the request is unsafe or unclear."
    )
