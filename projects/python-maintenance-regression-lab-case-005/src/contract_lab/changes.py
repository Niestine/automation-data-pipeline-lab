"""Reader-conditional OpenAPI diffs.

Change ids are emitted before any version string is consulted. Each id maps to
breaking, non-breaking, or undecidable twice: once for a strict reader and
once for a tolerant reader. Unlisted edits stay undecidable.

Removal after a published sunset stays undecidable. The source extract does
not give that cell. Removal at the sunset instant uses that same cell, because
"before" is strict.

Type detectors are disjoint:

- a proper subset or superset of a type list is narrowed or widened
- ``integer`` to ``number`` is ``type-compatible``
- dropping the ``type`` keyword is ``type-generalized``
- adding the ``type`` keyword is ``type-specialized`` on a response and
  ``type-changed`` on a request (the archived request section has no
  specialized id)
- any other single-type pair is ``type-changed``
- adding or changing ``enum`` is ``type_changed_to_enum``

An oasdiff level is not a Serbout impact. ``impact_for`` keeps those ids
undecidable except for the type-modification rules below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from contract_lab.errors import SunsetTimingError
from contract_lab.http_lifecycle import parse_sunset

METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

_MISSING = object()

# reader -> impact. Unknown ids are undecidable.
_STRUCTURAL: dict[str, dict[str, str]] = {
    "path_added": {"strict": "non_breaking", "tolerant": "non_breaking"},
    "operation_added": {"strict": "non_breaking", "tolerant": "non_breaking"},
    "path_removed_no_notice": {"strict": "breaking", "tolerant": "breaking"},
    "operation_removed_no_notice": {"strict": "breaking", "tolerant": "breaking"},
    "path_removed_before_sunset": {"strict": "breaking", "tolerant": "breaking"},
    "operation_removed_before_sunset": {"strict": "breaking", "tolerant": "breaking"},
    "path_removed_after_sunset": {"strict": "undecidable", "tolerant": "undecidable"},
    "operation_removed_after_sunset": {"strict": "undecidable", "tolerant": "undecidable"},
    "path_removed_after_deprecation": {"strict": "breaking", "tolerant": "breaking"},
    "operation_removed_after_deprecation": {"strict": "breaking", "tolerant": "breaking"},
    "response_property_added": {"strict": "breaking", "tolerant": "non_breaking"},
    "response_property_deleted": {"strict": "breaking", "tolerant": "breaking"},
    "request_parameter_added": {"strict": "breaking", "tolerant": "breaking"},
    "required_element_added": {"strict": "breaking", "tolerant": "breaking"},
    "type_modified": {"strict": "breaking", "tolerant": "undecidable"},
    "type_changed_to_enum": {"strict": "breaking", "tolerant": "undecidable"},
    "nullable_flag_changed": {"strict": "undecidable", "tolerant": "undecidable"},
    "optional_flag_changed": {"strict": "undecidable", "tolerant": "undecidable"},
}


def impact_for(change_id: str, reader: str) -> str:
    if reader not in {"strict", "tolerant"}:
        raise ValueError(reader)
    structural = _STRUCTURAL.get(change_id)
    if structural is not None:
        return structural[reader]
    if change_id == "response-body-type-compatible":
        # Type modification is breaking for the strict reader. The reviewed
        # compatible response-body rule is the tolerant exception.
        return "breaking" if reader == "strict" else "non_breaking"
    if change_id.endswith("-type-changed"):
        return "breaking" if reader == "strict" else "undecidable"
    return "undecidable"


def _methods(path_item: dict) -> dict[str, dict]:
    found = {}
    for key, value in path_item.items():
        if key.lower() in METHODS and isinstance(value, dict):
            found[key.lower()] = value
    return found


def _media_schema(container: dict | None) -> object:
    if not isinstance(container, dict):
        return _MISSING
    content = container.get("content")
    if not isinstance(content, dict):
        return _MISSING
    media = content.get("application/json")
    if not isinstance(media, dict) or "schema" not in media:
        return _MISSING
    return media["schema"]


def _response_schema(operation: dict) -> object:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return _MISSING
    return _media_schema(responses.get("200"))


def _request_schema(operation: dict) -> object:
    return _media_schema(operation.get("requestBody") if isinstance(operation.get("requestBody"), dict) else None)


def removal_suffix(operations: list[dict], after_doc: dict) -> str:
    sunsets: list[datetime] = []
    deprecated = False
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if operation.get("deprecated") is True:
            deprecated = True
        raw = operation.get("x-sunset")
        if raw is not None:
            sunsets.append(parse_sunset(str(raw)))
    if sunsets:
        if "x-observed-at" not in after_doc:
            raise SunsetTimingError("x-observed-at is required when a removed operation has x-sunset")
        observed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=int(after_doc["x-observed-at"]))
        if observed < min(sunsets):
            return "before_sunset"
        return "after_sunset"
    if deprecated:
        return "after_deprecation"
    return "no_notice"


def _is_bool_schema(schema) -> bool:
    return isinstance(schema, bool)


def _bool_ids(before, after, location: str) -> list[str]:
    before_false = before is False
    after_false = after is False
    if not before_false and after_false:
        return [f"{location}-schema-became-false"]
    if before_false and not after_false:
        return [f"{location}-schema-became-not-false"]
    return []


def _enum_changed(before: dict, after: dict) -> bool:
    if "enum" not in before and "enum" not in after:
        return False
    return before.get("enum") != after.get("enum")


def _nullable_changed(before: dict, after: dict) -> bool:
    if "nullable" not in before and "nullable" not in after:
        return False
    return before.get("nullable") != after.get("nullable")


def diff_type(before: dict, after: dict, location: str) -> list[str]:
    before_type = before.get("type", _MISSING)
    after_type = after.get("type", _MISSING)
    if before_type == after_type:
        return []
    # Dropping or adding the keyword is checked first, for a single type and a
    # type list alike.
    if before_type is not _MISSING and after_type is _MISSING:
        return [f"{location}-type-generalized"]
    if before_type is _MISSING and after_type is not _MISSING:
        if location.startswith("response"):
            return [f"{location}-type-specialized"]
        return [f"{location}-type-changed"]
    if isinstance(before_type, list) or isinstance(after_type, list):
        before_set = set(before_type if isinstance(before_type, list) else [before_type])
        after_set = set(after_type if isinstance(after_type, list) else [after_type])
        if after_set < before_set:
            return [f"{location}-list-of-types-narrowed"]
        if after_set > before_set:
            return [f"{location}-list-of-types-widened"]
        if before_set != after_set:
            return [f"{location}-type-changed"]
        return []
    if (before_type, after_type) == ("integer", "number"):
        return [f"{location}-type-compatible"]
    if before_type != after_type:
        return [f"{location}-type-changed"]
    return []


def _mutability(before: dict, after: dict, side: str, required_before: bool, required_after: bool) -> list[str]:
    if required_before != required_after:
        return []
    scope = "required" if required_before else "optional"
    ids: list[str] = []
    before_read = bool(before.get("readOnly"))
    after_read = bool(after.get("readOnly"))
    before_write = bool(before.get("writeOnly"))
    after_write = bool(after.get("writeOnly"))
    if before_read != after_read:
        word = "read-only" if after_read else "not-read-only"
        ids.append(f"{side}-{scope}-property-became-{word}")
    if before_write != after_write:
        word = "write-only" if after_write else "not-write-only"
        ids.append(f"{side}-{scope}-property-became-{word}")
    return ids


def _keyword_changed(before: dict, after: dict, key: str) -> bool:
    if key not in before and key not in after:
        return False
    return before.get(key) != after.get(key)


def diff_property(before, after, side: str, required_before: bool, required_after: bool) -> list[str]:
    location = f"{side}-property"
    if _is_bool_schema(before) or _is_bool_schema(after):
        return _bool_ids(before, after, location)
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["undecidable_schema"]
    ids: list[str] = []
    if _keyword_changed(before, after, "contentEncoding"):
        ids.append(f"{location}-content-encoding-changed")
    if _keyword_changed(before, after, "contentMediaType"):
        ids.append(f"{location}-content-media-type-changed")
    ids.extend(_mutability(before, after, side, required_before, required_after))
    ids.extend(diff_type(before, after, location))
    if _enum_changed(before, after):
        ids.append("type_changed_to_enum")
    if _nullable_changed(before, after):
        ids.append("nullable_flag_changed")
    return ids


def diff_properties(before: dict, after: dict, side: str) -> list[str]:
    if (
        "properties" not in before
        and "properties" not in after
        and "required" not in before
        and "required" not in after
    ):
        return []
    ids: list[str] = []
    before_props = before.get("properties") or {}
    after_props = after.get("properties") or {}
    before_required = set(before.get("required") or [])
    after_required = set(after.get("required") or [])
    for name in sorted(set(before_props) | set(after_props)):
        if name not in before_props:
            ids.append("response_property_added" if side == "response" else "request_property_added")
        elif name not in after_props:
            ids.append("response_property_deleted" if side == "response" else "request_property_deleted")
        else:
            ids.extend(
                diff_property(
                    before_props[name],
                    after_props[name],
                    side,
                    name in before_required,
                    name in after_required,
                )
            )
    for _name in sorted(after_required - before_required):
        ids.append("required_element_added")
    for _name in sorted(before_required - after_required):
        ids.append("optional_flag_changed")
    return ids


def diff_body(before, after, side: str) -> list[str]:
    location = f"{side}-body"
    if _is_bool_schema(before) or _is_bool_schema(after):
        return _bool_ids(before, after, location)
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["undecidable_schema"]
    ids: list[str] = []
    ids.extend(diff_type(before, after, location))
    if _enum_changed(before, after):
        ids.append("type_changed_to_enum")
    if _nullable_changed(before, after):
        ids.append("nullable_flag_changed")
    ids.extend(diff_properties(before, after, side))
    return ids


def _parameter_key(parameter: dict) -> tuple[str, str]:
    return (str(parameter.get("in") or ""), str(parameter.get("name") or ""))


def diff_parameters(before: list, after: list) -> list[str]:
    ids: list[str] = []
    before_map = {_parameter_key(item): item for item in before if isinstance(item, dict)}
    after_map = {_parameter_key(item): item for item in after if isinstance(item, dict)}
    for key in sorted(set(before_map) | set(after_map)):
        if key not in before_map:
            ids.append("request_parameter_added")
            continue
        if key not in after_map:
            ids.append("request_parameter_removed")
            continue
        left = before_map[key]
        right = after_map[key]
        left_schema = left.get("schema") if isinstance(left.get("schema"), dict) else {}
        right_schema = right.get("schema") if isinstance(right.get("schema"), dict) else {}
        if left.get("schema") != right.get("schema") and (
            "schema" in left or "schema" in right
        ):
            type_ids = diff_type(left_schema, right_schema, "request-parameter")
            if type_ids:
                ids.append("type_modified")
            elif _enum_changed(left_schema, right_schema):
                ids.append("type_changed_to_enum")
        left_required = bool(left.get("required"))
        right_required = bool(right.get("required"))
        if not left_required and right_required:
            ids.append("required_element_added")
        elif left_required and not right_required:
            ids.append("optional_flag_changed")
    return ids


def diff_operation(before: dict, after: dict) -> list[str]:
    ids: list[str] = []
    if _keyword_changed(before, after, "security"):
        ids.append("security_changed")
    ids.extend(diff_parameters(before.get("parameters") or [], after.get("parameters") or []))
    before_request = _request_schema(before)
    after_request = _request_schema(after)
    if before_request is not _MISSING or after_request is not _MISSING:
        if before_request is _MISSING:
            ids.append("request_body_added")
        elif after_request is _MISSING:
            ids.append("request_body_removed")
        else:
            ids.extend(diff_body(before_request, after_request, "request"))
    before_response = _response_schema(before)
    after_response = _response_schema(after)
    if before_response is not _MISSING or after_response is not _MISSING:
        if before_response is _MISSING:
            ids.append("response_schema_added")
        elif after_response is _MISSING:
            ids.append("response_schema_removed")
        else:
            ids.extend(diff_body(before_response, after_response, "response"))
    before_status = set((before.get("responses") or {}).keys())
    after_status = set((after.get("responses") or {}).keys())
    if before_status != after_status:
        ids.append("response_status_changed")
    return ids


def diff_openapi(before: dict, after: dict) -> list[str]:
    """Structural ids only. ``info``, servers, tags, and the OAS version are ignored."""

    ids: list[str] = []
    before_paths = before.get("paths") or {}
    after_paths = after.get("paths") or {}
    for path in sorted(set(before_paths) | set(after_paths)):
        if path not in before_paths:
            ids.append("path_added")
            continue
        if path not in after_paths:
            operations = list(_methods(before_paths[path]).values())
            ids.append("path_removed_" + removal_suffix(operations, after))
            continue
        before_methods = _methods(before_paths[path])
        after_methods = _methods(after_paths[path])
        for method in sorted(set(before_methods) | set(after_methods)):
            if method not in before_methods:
                ids.append("operation_added")
                continue
            if method not in after_methods:
                ids.append(
                    "operation_removed_" + removal_suffix([before_methods[method]], after)
                )
                continue
            ids.extend(diff_operation(before_methods[method], after_methods[method]))
    if _keyword_changed(before, after, "security"):
        ids.append("security_changed")
    return ids
