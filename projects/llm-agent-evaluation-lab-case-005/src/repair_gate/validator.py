"""Draft 2020-12 subset validator and strict-channel admission.

Format is an annotation unless format_assertion is on. The default is off.
The validator never reads a candidate's own ``valid`` or ``confidence`` field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .pointer import PointerError, pointer_get
from .util import is_number, json_type_name

COMPOSITION_KEYS = ("allOf", "not", "dependentRequired", "if", "then", "else")
VALIDATOR_OWNED = (
    "pattern",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
)
KNOWN_FORMATS = ("date-time", "date", "time", "email", "uri", "uuid", "regex")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


@dataclass
class Admission:
    declared_strict: int
    declared_closed: int
    strict_rejected: bool
    reasons: list[str] = field(default_factory=list)
    validator_owned: list[dict[str, str]] = field(default_factory=list)
    object_problems: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared_strict": self.declared_strict,
            "declared_closed": self.declared_closed,
            "strict_rejected": self.strict_rejected,
            "reasons": list(self.reasons),
            "validator_owned": list(self.validator_owned),
            "object_problems": list(self.object_problems),
        }


@dataclass
class ValidationResult:
    valid: bool
    flag: dict[str, bool]
    basic: list[dict[str, Any]]
    detailed: dict[str, Any]
    annotations: list[dict[str, Any]]
    errors: list[dict[str, Any]]


def _absolute(root_id: str, key_pointer: str) -> str:
    if not root_id:
        return key_pointer
    return f"{root_id}#{key_pointer}"


def _error(
    keyword: str,
    instance_pointer: str,
    key_pointer: str,
    root_id: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    unit = {
        "valid": False,
        "keyword": keyword,
        "instanceLocation": instance_pointer,
        "keywordLocation": key_pointer,
        "absoluteKeywordLocation": _absolute(root_id, key_pointer),
        "message": message,
        "children": [],
    }
    unit.update(extra)
    return unit


def _type_ok(value: Any, expected: str) -> bool:
    actual = json_type_name(value)
    if expected == "number":
        return actual in ("integer", "number")
    if expected == "integer":
        return actual == "integer"
    return actual == expected


def _is_date(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%d") == value


def _is_time(value: str) -> bool:
    match = _TIME.fullmatch(value)
    if not match:
        return False
    hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return hour <= 23 and minute <= 59 and second <= 60


def _is_date_time(value: str) -> bool:
    if "T" not in value:
        return False
    date_part, time_part = value.split("T", 1)
    return _is_date(date_part) and _is_time(time_part)


def _format_holds(fmt: str, value: str) -> bool | None:
    """Return True/False for known formats, None when the format is unknown."""
    if fmt == "date-time":
        return _is_date_time(value)
    if fmt == "date":
        return _is_date(value)
    if fmt == "time":
        return _is_time(value)
    if fmt == "email":
        return _EMAIL.fullmatch(value) is not None
    if fmt == "uri":
        return _URI.fullmatch(value) is not None
    if fmt == "uuid":
        return _UUID.fullmatch(value) is not None
    if fmt == "regex":
        try:
            re.compile(value)
        except re.error:
            return False
        return True
    return None


def _resolve_ref(root: Any, ref: str) -> tuple[str, Any]:
    if ref == "#":
        return "", root
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise PointerError("bad_ref", str(ref))
    pointer = ref[1:]
    return pointer, pointer_get(root, pointer)


def _eval(
    schema: Any,
    instance: Any,
    inst_ptr: str,
    key_ptr: str,
    root_id: str,
    root: Any,
    errors: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    format_assertion: bool,
    resolving: tuple[str, ...],
) -> None:
    if schema is True:
        return
    if schema is False:
        errors.append(_error("schema", inst_ptr, key_ptr, root_id, "schema is false"))
        return
    if not isinstance(schema, dict):
        errors.append(_error("schema", inst_ptr, key_ptr, root_id, "schema is not an object"))
        return

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in resolving:
            errors.append(_error("$ref", inst_ptr, key_ptr + "/$ref", root_id, "cyclic $ref"))
            return
        try:
            target_ptr, target = _resolve_ref(root, ref)
        except PointerError:
            errors.append(_error("$ref", inst_ptr, key_ptr + "/$ref", root_id, "unresolved $ref"))
            return
        _eval(
            target,
            instance,
            inst_ptr,
            target_ptr,
            root_id,
            root,
            errors,
            annotations,
            format_assertion,
            resolving + (ref,),
        )
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        if siblings:
            _eval(
                siblings,
                instance,
                inst_ptr,
                key_ptr,
                root_id,
                root,
                errors,
                annotations,
                format_assertion,
                resolving,
            )
        return

    if "type" in schema:
        spec = schema["type"]
        allowed = spec if isinstance(spec, list) else [spec]
        if not any(isinstance(item, str) and _type_ok(instance, item) for item in allowed):
            errors.append(
                _error(
                    "type",
                    inst_ptr,
                    key_ptr + "/type",
                    root_id,
                    "type mismatch",
                    allowed=allowed,
                )
            )

    if "const" in schema:
        from .util import json_equal

        if not json_equal(instance, schema["const"]):
            errors.append(_error("const", inst_ptr, key_ptr + "/const", root_id, "const mismatch"))

    if "enum" in schema:
        from .util import json_equal

        if not any(json_equal(instance, item) for item in schema["enum"]):
            errors.append(_error("enum", inst_ptr, key_ptr + "/enum", root_id, "enum mismatch"))

    if is_number(instance):
        for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if keyword not in schema:
                continue
            bound = schema[keyword]
            if not is_number(bound):
                continue
            failed = (
                (keyword == "minimum" and instance < bound)
                or (keyword == "maximum" and instance > bound)
                or (keyword == "exclusiveMinimum" and instance <= bound)
                or (keyword == "exclusiveMaximum" and instance >= bound)
            )
            if failed:
                errors.append(
                    _error(
                        keyword,
                        inst_ptr,
                        key_ptr + "/" + keyword,
                        root_id,
                        f"{keyword} failed",
                        bound=bound,
                    )
                )

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(
                _error(
                    "minLength",
                    inst_ptr,
                    key_ptr + "/minLength",
                    root_id,
                    "minLength failed",
                    bound=schema["minLength"],
                )
            )
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(
                _error(
                    "maxLength",
                    inst_ptr,
                    key_ptr + "/maxLength",
                    root_id,
                    "maxLength failed",
                    bound=schema["maxLength"],
                )
            )
        if "pattern" in schema:
            try:
                matched = re.search(schema["pattern"], instance) is not None
            except re.error:
                matched = False
            if not matched:
                errors.append(
                    _error("pattern", inst_ptr, key_ptr + "/pattern", root_id, "pattern failed")
                )
        if "format" in schema:
            fmt = schema["format"]
            holds = _format_holds(fmt, instance) if isinstance(fmt, str) else None
            annotations.append(
                {
                    "keyword": "format",
                    "format": fmt,
                    "instanceLocation": inst_ptr,
                    "keywordLocation": key_ptr + "/format",
                    "asserted": bool(format_assertion and holds is not None),
                }
            )
            if format_assertion and holds is False:
                errors.append(
                    _error("format", inst_ptr, key_ptr + "/format", root_id, f"format {fmt} failed")
                )

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(
                _error(
                    "minItems",
                    inst_ptr,
                    key_ptr + "/minItems",
                    root_id,
                    "minItems failed",
                    bound=schema["minItems"],
                )
            )
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(
                _error(
                    "maxItems",
                    inst_ptr,
                    key_ptr + "/maxItems",
                    root_id,
                    "maxItems failed",
                    bound=schema["maxItems"],
                )
            )
        if schema.get("uniqueItems") is True:
            from .util import json_equal

            for left_i, left in enumerate(instance):
                for right in instance[left_i + 1 :]:
                    if json_equal(left, right):
                        errors.append(
                            _error(
                                "uniqueItems",
                                inst_ptr,
                                key_ptr + "/uniqueItems",
                                root_id,
                                "duplicate item",
                            )
                        )
                        break
                else:
                    continue
                break
        if "items" in schema:
            for index, item in enumerate(instance):
                _eval(
                    schema["items"],
                    item,
                    f"{inst_ptr}/{index}",
                    key_ptr + "/items",
                    root_id,
                    root,
                    errors,
                    annotations,
                    format_assertion,
                    resolving,
                )

    if isinstance(instance, dict):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [name for name in required if isinstance(name, str) and name not in instance]
            if missing:
                errors.append(
                    _error(
                        "required",
                        inst_ptr,
                        key_ptr + "/required",
                        root_id,
                        "missing required",
                        missing=missing,
                    )
                )
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        additional = schema.get("additionalProperties", True)
        unexpected = [key for key in instance if key not in properties]
        if additional is False and unexpected:
            errors.append(
                _error(
                    "additionalProperties",
                    inst_ptr,
                    key_ptr + "/additionalProperties",
                    root_id,
                    "unexpected properties",
                    unexpected=unexpected,
                )
            )
        elif isinstance(additional, dict):
            for key in unexpected:
                token = key.replace("~", "~0").replace("/", "~1")
                _eval(
                    additional,
                    instance[key],
                    f"{inst_ptr}/{token}",
                    key_ptr + "/additionalProperties",
                    root_id,
                    root,
                    errors,
                    annotations,
                    format_assertion,
                    resolving,
                )
        for name, sub in properties.items():
            if name not in instance:
                continue
            token = name.replace("~", "~0").replace("/", "~1")
            _eval(
                sub,
                instance[name],
                f"{inst_ptr}/{token}",
                f"{key_ptr}/properties/{token}",
                root_id,
                root,
                errors,
                annotations,
                format_assertion,
                resolving,
            )
        if "dependentRequired" in schema and isinstance(schema["dependentRequired"], dict):
            for prop, needs in schema["dependentRequired"].items():
                if prop in instance and isinstance(needs, list):
                    missing = [name for name in needs if name not in instance]
                    if missing:
                        errors.append(
                            _error(
                                "dependentRequired",
                                inst_ptr,
                                key_ptr + "/dependentRequired",
                                root_id,
                                "dependent required missing",
                                missing=missing,
                            )
                        )

    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        matched = False
        branch_errors: list[list[dict[str, Any]]] = []
        for index, sub in enumerate(schema["anyOf"]):
            sub_errors: list[dict[str, Any]] = []
            _eval(
                sub,
                instance,
                inst_ptr,
                f"{key_ptr}/anyOf/{index}",
                root_id,
                root,
                sub_errors,
                annotations,
                format_assertion,
                resolving,
            )
            if not sub_errors:
                matched = True
                break
            branch_errors.append(sub_errors)
        if not matched:
            parent = _error("anyOf", inst_ptr, key_ptr + "/anyOf", root_id, "anyOf failed")
            for sub_errors in branch_errors:
                parent["children"].extend(sub_errors)
            errors.append(parent)

    if "allOf" in schema and isinstance(schema["allOf"], list):
        child_errors: list[dict[str, Any]] = []
        for index, sub in enumerate(schema["allOf"]):
            _eval(
                sub,
                instance,
                inst_ptr,
                f"{key_ptr}/allOf/{index}",
                root_id,
                root,
                child_errors,
                annotations,
                format_assertion,
                resolving,
            )
        if child_errors:
            parent = _error("allOf", inst_ptr, key_ptr + "/allOf", root_id, "allOf failed")
            parent["children"] = child_errors
            errors.append(parent)

    if "not" in schema:
        sub_errors = []
        _eval(
            schema["not"],
            instance,
            inst_ptr,
            key_ptr + "/not",
            root_id,
            root,
            sub_errors,
            annotations,
            format_assertion,
            resolving,
        )
        if not sub_errors:
            errors.append(_error("not", inst_ptr, key_ptr + "/not", root_id, "not failed"))

    if "if" in schema:
        if_errors: list[dict[str, Any]] = []
        _eval(
            schema["if"],
            instance,
            inst_ptr,
            key_ptr + "/if",
            root_id,
            root,
            if_errors,
            annotations,
            format_assertion,
            resolving,
        )
        branch = "then" if not if_errors else "else"
        if branch in schema:
            _eval(
                schema[branch],
                instance,
                inst_ptr,
                key_ptr + "/" + branch,
                root_id,
                root,
                errors,
                annotations,
                format_assertion,
                resolving,
            )


def _flatten_basic(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []

    def walk(unit: dict[str, Any]) -> None:
        flat.append(
            {
                "valid": False,
                "keyword": unit["keyword"],
                "instanceLocation": unit["instanceLocation"],
                "keywordLocation": unit["keywordLocation"],
                "absoluteKeywordLocation": unit["absoluteKeywordLocation"],
                "error": unit["message"],
            }
        )
        for child in unit.get("children") or []:
            walk(child)

    for unit in errors:
        walk(unit)
    return flat


def _detailed(errors: list[dict[str, Any]], root_id: str, valid: bool) -> dict[str, Any]:
    def pack(unit: dict[str, Any]) -> dict[str, Any]:
        return {
            "valid": False,
            "keyword": unit["keyword"],
            "instanceLocation": unit["instanceLocation"],
            "keywordLocation": unit["keywordLocation"],
            "absoluteKeywordLocation": unit["absoluteKeywordLocation"],
            "error": unit["message"],
            "errors": [pack(child) for child in unit.get("children") or []],
        }

    return {
        "valid": valid,
        "keywordLocation": "",
        "instanceLocation": "",
        "absoluteKeywordLocation": _absolute(root_id, ""),
        "errors": [pack(unit) for unit in errors],
    }


def _apply_contracts(
    instance: Any,
    contracts: list[dict[str, Any]] | None,
    errors: list[dict[str, Any]],
    root_id: str,
) -> None:
    # ``valid`` and ``confidence`` on the instance are intentionally unread.
    if not contracts:
        return
    for index, contract in enumerate(contracts):
        pointer = contract.get("pointer", "")
        key_ptr = f"/x-contract/{index}"
        try:
            value = pointer_get(instance, pointer)
        except PointerError:
            errors.append(
                _error("x-contract", pointer, key_ptr, root_id, "contract pointer missing")
            )
            continue
        if "maximum" in contract:
            if not is_number(value) or value > contract["maximum"]:
                errors.append(
                    _error(
                        "x-contract",
                        pointer,
                        key_ptr,
                        root_id,
                        "contract maximum failed",
                        bound=contract["maximum"],
                    )
                )


def validate(
    schema: Any,
    instance: Any,
    *,
    format_assertion: bool = False,
    contracts: list[dict[str, Any]] | None = None,
) -> ValidationResult:
    errors: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    root_id = schema.get("$id", "") if isinstance(schema, dict) else ""
    _eval(
        schema,
        instance,
        "",
        "",
        root_id,
        schema,
        errors,
        annotations,
        format_assertion,
        (),
    )
    _apply_contracts(instance, contracts, errors, root_id)
    valid = not errors
    return ValidationResult(
        valid=valid,
        flag={"valid": valid},
        basic=_flatten_basic(errors),
        detailed=_detailed(errors, root_id, valid),
        annotations=annotations,
        errors=errors,
    )


def _walk_admission(node: Any, pointer: str, seen: set[str], admission_state: dict[str, list]) -> None:
    if not isinstance(node, dict):
        return
    if pointer in seen:
        return
    seen.add(pointer)
    for key in COMPOSITION_KEYS:
        if key in node:
            admission_state["reasons"].append(f"{pointer}/{key}".replace("//", "/"))
    for key in VALIDATOR_OWNED:
        if key in node:
            location = f"{pointer}/{key}".replace("//", "/")
            admission_state["owned"].append({"keyword": key, "location": location})
    declared = node.get("type")
    is_object = declared == "object" or (isinstance(declared, list) and "object" in declared)
    if is_object or isinstance(node.get("properties"), dict):
        properties = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        required = node.get("required", [] if not properties else None)
        if node.get("additionalProperties") is not False:
            admission_state["objects"].append(
                {"location": pointer or "/", "problem": "additionalProperties"}
            )
        if (
            not isinstance(required, list)
            or len(required) != len(set(required))
            or set(required) != set(properties)
        ):
            admission_state["objects"].append({"location": pointer or "/", "problem": "required"})
        for name, sub in properties.items():
            token = name.replace("~", "~0").replace("/", "~1")
            _walk_admission(sub, f"{pointer}/properties/{token}", seen, admission_state)
    if isinstance(node.get("$ref"), str) and node["$ref"].startswith("#"):
        # Local ref targets are walked by _follow_refs.
        return
    for key in ("items", "additionalProperties", "not", "if", "then", "else"):
        if isinstance(node.get(key), dict):
            _walk_admission(node[key], f"{pointer}/{key}", seen, admission_state)
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(node.get(key), list):
            for index, sub in enumerate(node[key]):
                _walk_admission(sub, f"{pointer}/{key}/{index}", seen, admission_state)
    if isinstance(node.get("$defs"), dict):
        for name, sub in node["$defs"].items():
            _walk_admission(sub, f"{pointer}/$defs/{name}", seen, admission_state)


def admit(schema: Any) -> Admission:
    state: dict[str, list] = {"reasons": [], "owned": [], "objects": []}
    if isinstance(schema, dict):
        seen: set[str] = set()
        _walk_admission(schema, "", seen, state)
        _follow_refs(schema, schema, state, seen)
    strict_rejected = bool(state["reasons"])
    object_blocked = bool(state["objects"])
    declared_strict = 0 if strict_rejected or object_blocked else 1
    declared_closed = 1 if declared_strict == 1 and not state["owned"] else 0
    reasons = list(state["reasons"])
    if object_blocked:
        reasons.append("strict object lint failed")
    return Admission(
        declared_strict=declared_strict,
        declared_closed=declared_closed,
        strict_rejected=strict_rejected or object_blocked,
        reasons=reasons,
        validator_owned=list(state["owned"]),
        object_problems=list(state["objects"]),
    )


def _follow_refs(node: Any, root: Any, state: dict[str, list], seen: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            try:
                target_ptr, target = _resolve_ref(root, ref)
            except PointerError:
                state["reasons"].append("unresolved $ref")
                target = None
                target_ptr = ""
            if target is not None:
                _walk_admission(target, target_ptr, seen, state)
        for value in node.values():
            _follow_refs(value, root, state, seen)
    elif isinstance(node, list):
        for value in node:
            _follow_refs(value, root, state, seen)


def schema_types_at(schema: Any, pointer: str) -> set[str] | None:
    """JSON types declared at a concrete instance pointer, when they can be read off."""
    from .pointer import parse_pointer

    node: Any = schema
    if isinstance(node, dict) and "$ref" in node and len(node) == 1:
        try:
            _, node = _resolve_ref(schema, node["$ref"])
        except PointerError:
            return None
    tokens = parse_pointer(pointer)
    for token in tokens:
        if not isinstance(node, dict):
            return None
        if "$ref" in node:
            try:
                _, node = _resolve_ref(schema, node["$ref"])
            except PointerError:
                return None
        if isinstance(node, dict) and "properties" in node and token in node.get("properties", {}):
            node = node["properties"][token]
            continue
        if isinstance(node, dict) and "items" in node and is_index_token(token):
            node = node["items"]
            continue
        if isinstance(node, dict) and isinstance(node.get("additionalProperties"), dict):
            node = node["additionalProperties"]
            continue
        return None
    if isinstance(node, dict) and "$ref" in node:
        try:
            _, node = _resolve_ref(schema, node["$ref"])
        except PointerError:
            return None
    if not isinstance(node, dict) or "type" not in node:
        return None
    spec = node["type"]
    if isinstance(spec, str):
        return {spec}
    if isinstance(spec, list):
        return {item for item in spec if isinstance(item, str)}
    return None


def is_index_token(token: str) -> bool:
    return token.isdigit()
