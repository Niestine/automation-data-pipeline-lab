"""Closed subset of JSON Schema draft 2020-12.

Unknown keywords are annotations at instance-validation time. They fail
`audit_schema`, which is the stand-in for checking a schema against the
2020-12 meta-schema: a misspelled guard must not pass review silently.
`format` is collected and is not an assertion. `$comment` is never executed.
"""

from __future__ import annotations

from typing import Any

from fieldlog.support import SchemaRejected

MAX_DEPTH = 32
DIALECT = "https://json-schema.org/draft/2020-12/schema"

KNOWN_KEYWORDS = {
    "$schema",
    "$id",
    "$comment",
    "$defs",
    "title",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "enum",
    "const",
    "oneOf",
    "prefixItems",
    "items",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "minLength",
    "format",
}

_TYPE_NAMES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def audit_schema(schema: Any, depth: int = 0, path: str = "") -> dict:
    """Reject unknown keywords, non-schemas, and recursion past the bound."""

    errors: list[dict] = []
    _audit(schema, depth, path, errors)
    return {"valid": not errors, "errors": errors}


def require_audit(schema: Any) -> None:
    result = audit_schema(schema)
    if not result["valid"]:
        raise SchemaRejected(result["errors"][0]["error"])


def assert_dialect(schema: dict) -> None:
    if schema.get("$schema") != DIALECT:
        raise SchemaRejected("shipped schema must declare the 2020-12 dialect")
    if not isinstance(schema.get("$id"), str) or not schema["$id"]:
        raise SchemaRejected("shipped schema must declare a stable $id")


def validate(instance: Any, schema: Any, output: str = "detailed") -> dict:
    """Validate one instance. `detailed` includes keywordLocation and instanceLocation."""

    if output not in {"flag", "basic", "detailed"}:
        raise SchemaRejected(f"unsupported output format: {output}")
    errors: list[dict] = []
    annotations: list[dict] = []
    _eval(instance, schema, [], [], errors, annotations, 0)
    valid = not errors
    if output == "flag":
        return {"valid": valid}
    unit = {
        "valid": valid,
        "keywordLocation": "",
        "instanceLocation": "",
        "errors": errors,
        "annotations": annotations,
    }
    return unit


def _audit(schema: Any, depth: int, path: str, errors: list[dict]) -> None:
    if depth > MAX_DEPTH:
        errors.append(_err([path or "schema"], [], "Schema recursion bound exceeded."))
        return
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        errors.append(_err([path or "schema"], [], "Schema must be an object or a boolean."))
        return
    for key in schema:
        if key not in KNOWN_KEYWORDS:
            errors.append(
                {
                    "valid": False,
                    "keywordLocation": _pointer([key] if not path else [path, key]),
                    "instanceLocation": "",
                    "error": f"Unknown keyword {key!r} is not in the lab dialect.",
                }
            )
    for key, sub in schema.get("properties", {}).items() if isinstance(schema.get("properties"), dict) else []:
        _audit(sub, depth + 1, key, errors)
    items = schema.get("items")
    if isinstance(items, (dict, bool)):
        _audit(items, depth + 1, "items", errors)
    for index, sub in enumerate(schema.get("prefixItems", []) if isinstance(schema.get("prefixItems"), list) else []):
        _audit(sub, depth + 1, f"prefixItems/{index}", errors)
    for index, sub in enumerate(schema.get("oneOf", []) if isinstance(schema.get("oneOf"), list) else []):
        _audit(sub, depth + 1, f"oneOf/{index}", errors)
    extra = schema.get("additionalProperties")
    if isinstance(extra, (dict, bool)):
        _audit(extra, depth + 1, "additionalProperties", errors)


def _eval(instance, schema, inst_path, key_path, errors, annotations, depth) -> None:
    if depth > MAX_DEPTH:
        errors.append(_err(key_path, inst_path, "Schema recursion bound exceeded."))
        return
    if schema is True or schema is False:
        if schema is False:
            errors.append(_err(key_path, inst_path, "Boolean schema false rejects every instance."))
        return
    if not isinstance(schema, dict):
        errors.append(_err(key_path, inst_path, "Schema must be an object or a boolean."))
        return

    for key, value in schema.items():
        if key not in KNOWN_KEYWORDS:
            annotations.append(
                {
                    "keyword": key,
                    "keywordLocation": _pointer(key_path + [key]),
                    "value": value,
                }
            )
    if "$comment" in schema:
        annotations.append(
            {
                "keyword": "$comment",
                "keywordLocation": _pointer(key_path + ["$comment"]),
                "value": schema["$comment"],
                "executed": False,
            }
        )
    if "format" in schema:
        annotations.append(
            {
                "keyword": "format",
                "keywordLocation": _pointer(key_path + ["format"]),
                "value": schema["format"],
                "asserted": False,
            }
        )

    if "type" in schema:
        expected = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, name) for name in expected):
            errors.append(_err(key_path + ["type"], inst_path, f"Expected type {schema['type']}."))
            return

    if "const" in schema and instance != schema["const"]:
        errors.append(_err(key_path + ["const"], inst_path, "Value did not equal const."))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(_err(key_path + ["enum"], inst_path, "Value is not in enum."))
    if "minLength" in schema and isinstance(instance, str) and len(instance) < schema["minLength"]:
        errors.append(_err(key_path + ["minLength"], inst_path, "String is shorter than minLength."))
    if "minimum" in schema and _is_number(instance) and instance < schema["minimum"]:
        errors.append(_err(key_path + ["minimum"], inst_path, "Number is below minimum."))
    if "maximum" in schema and _is_number(instance) and instance > schema["maximum"]:
        errors.append(_err(key_path + ["maximum"], inst_path, "Number is above maximum."))

    if isinstance(instance, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for name in required:
            if name not in instance:
                errors.append(
                    _err(key_path + ["required"], inst_path, f"Required property {name!r} not found.")
                )
        additional = schema.get("additionalProperties", True)
        for name, value in instance.items():
            if name in properties:
                _eval(value, properties[name], inst_path + [name], key_path + ["properties", name], errors, annotations, depth + 1)
            elif additional is False:
                errors.append(
                    _err(
                        key_path + ["additionalProperties"],
                        inst_path + [name],
                        f"Additional property {name!r} found but was invalid.",
                    )
                )
            elif isinstance(additional, dict):
                _eval(
                    value,
                    additional,
                    inst_path + [name],
                    key_path + ["additionalProperties"],
                    errors,
                    annotations,
                    depth + 1,
                )

    if isinstance(instance, list):
        prefix = schema.get("prefixItems") if isinstance(schema.get("prefixItems"), list) else []
        for index, sub in enumerate(prefix):
            if index >= len(instance):
                break
            _eval(instance[index], sub, inst_path + [index], key_path + ["prefixItems", index], errors, annotations, depth + 1)
        if "items" in schema:
            item_schema = schema["items"]
            for index in range(len(prefix), len(instance)):
                if item_schema is False:
                    errors.append(
                        _err(key_path + ["items"], inst_path + [index], "Additional item is not allowed.")
                    )
                elif item_schema is not True:
                    _eval(
                        instance[index],
                        item_schema,
                        inst_path + [index],
                        key_path + ["items"],
                        errors,
                        annotations,
                        depth + 1,
                    )
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(_err(key_path + ["minItems"], inst_path, "Array is shorter than minItems."))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(_err(key_path + ["maxItems"], inst_path, "Array is longer than maxItems."))

    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        matches = 0
        for sub in schema["oneOf"]:
            branch_errors: list[dict] = []
            branch_notes: list[dict] = []
            _eval(instance, sub, inst_path, key_path + ["oneOf"], branch_errors, branch_notes, depth + 1)
            if not branch_errors:
                matches += 1
                annotations.extend(branch_notes)
        if matches != 1:
            errors.append(
                _err(
                    key_path + ["oneOf"],
                    inst_path,
                    f"Expected exactly one matching subschema, found {matches}.",
                )
            )


def _is_type(value: Any, name: str) -> bool:
    if name not in _TYPE_NAMES:
        return False
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _err(key_path, inst_path, message: str) -> dict:
    return {
        "valid": False,
        "keywordLocation": _pointer(key_path),
        "instanceLocation": _pointer(inst_path),
        "error": message,
    }


def _pointer(parts) -> str:
    out = ""
    for part in parts:
        text = str(part).replace("~", "~0").replace("/", "~1")
        out += "/" + text
    return out
