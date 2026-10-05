"""First-failure repair records. Alternatives come only from the failing keyword."""

from __future__ import annotations

from typing import Any

from .util import canonical

ALTERNATIVE_CAP = 12
ABLATIONS = ("raw", "loc_obs", "full_prose", "full_keyed")


def _record(
    label: str,
    location: str,
    observed: Any,
    alternatives: list[Any],
    status: str,
    *,
    observed_present: bool = True,
) -> dict[str, Any]:
    alternatives = list(alternatives)[:ALTERNATIVE_CAP]
    return {
        "label": label,
        "location": location,
        "observed": observed,
        "observed_present": observed_present,
        "alternatives": alternatives,
        "alternatives_status": status,
        "raw_text": (
            f"{label} failed at {location} observed {canonical(observed)}"
        ),
    }


def parse_failure() -> dict[str, Any]:
    return _record("json.parse", "", None, [], "unenumerated", observed_present=False)


def encode_with_schema(validation: Any, instance: Any, schema: Any) -> dict[str, Any]:
    if not validation.errors:
        return _record("none", "", None, [], "unenumerated", observed_present=False)
    unit = validation.errors[0]
    keyword = unit["keyword"]
    location = unit["instanceLocation"]
    observed, present = _observed(instance, location, unit)
    spec = _schema_at_keyword(schema, unit["keywordLocation"])

    if keyword == "enum":
        values = list(spec.get("enum", [])) if isinstance(spec, dict) else []
        return _record(keyword, location, observed, values, "enumerated", observed_present=present)
    if keyword == "const" and isinstance(spec, dict) and "const" in spec:
        return _record(keyword, location, observed, [spec["const"]], "enumerated", observed_present=present)
    if keyword == "type":
        allowed = unit.get("allowed")
        if not isinstance(allowed, list):
            allowed = []
        return _record(keyword, location, observed, list(allowed), "enumerated", observed_present=present)
    if keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems"):
        bound = unit.get("bound")
        return _record(
            keyword,
            location,
            observed,
            [{keyword: bound}],
            "enumerated",
            observed_present=present,
        )
    if keyword == "required":
        missing = list(unit.get("missing") or [])
        return _record(keyword, location, observed, missing, "enumerated", observed_present=present)
    if keyword == "additionalProperties":
        names = list(unit.get("unexpected") or [])
        return _record(keyword, location, observed, names, "enumerated", observed_present=present)
    if keyword == "x-contract":
        if "bound" in unit:
            return _record(
                keyword,
                location,
                observed,
                [{"maximum": unit["bound"]}],
                "enumerated",
                observed_present=present,
            )
        return _record(keyword, location, observed, [], "unenumerated", observed_present=present)
    if keyword in ("pattern", "format", "uniqueItems", "json.parse"):
        return _record(keyword, location, observed, [], "unenumerated", observed_present=present)
    if keyword == "anyOf" and isinstance(spec, dict):
        values = _consts_from_anyof(spec.get("anyOf"))
        status = "enumerated" if values else "unenumerated"
        return _record(keyword, location, observed, values, status, observed_present=present)
    return _record(keyword, location, observed, [], "unenumerated", observed_present=present)


def _observed(instance: Any, location: str, unit: dict[str, Any]) -> tuple[Any, bool]:
    from .pointer import PointerError, pointer_get

    if unit["keyword"] == "additionalProperties":
        names = list(unit.get("unexpected") or [])
        if isinstance(instance, dict):
            return {name: instance.get(name) for name in names}, True
    if unit["keyword"] == "required" and isinstance(instance, dict):
        return instance, True
    try:
        return pointer_get(instance, location), True
    except PointerError:
        return None, False


def _schema_at_keyword(schema: Any, keyword_location: str) -> Any:
    """Return the schema object that owns the failing keyword.

    keywordLocation points at the keyword (`/properties/code/enum`) or, after
    `$ref`, at the keyword inside the target. The owner is the parent object.
    """
    from .pointer import PointerError, pointer_get

    if not keyword_location:
        return schema
    parent, _, _leaf = keyword_location.rpartition("/")
    try:
        if parent == "":
            return schema
        return pointer_get(schema, parent)
    except PointerError:
        return {}


def _consts_from_anyof(branches: Any) -> list[Any]:
    found: list[Any] = []
    if not isinstance(branches, list):
        return found
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        if "const" in branch:
            found.append(branch["const"])
        elif isinstance(branch.get("enum"), list):
            found.extend(branch["enum"])
        if len(found) >= ALTERNATIVE_CAP:
            break
    return found


def render_repair(record: dict[str, Any] | None, ablation: str) -> Any:
    """What the next prompt is allowed to see for this ablation."""
    if record is None:
        return None
    if ablation not in ABLATIONS:
        raise ValueError(f"unknown ablation: {ablation}")
    if ablation == "raw":
        return record["raw_text"]
    if ablation == "loc_obs":
        return {
            "label": record["label"],
            "location": record["location"],
            "observed": record["observed"],
        }
    if ablation == "full_keyed":
        return {
            "label": record["label"],
            "location": record["location"],
            "observed": record["observed"],
            "alternatives": record["alternatives"],
            "alternatives_status": record["alternatives_status"],
        }
    return (
        f"Failure {record['label']} at {record['location']}. "
        f"Observed {canonical(record['observed'])}. "
        f"Alternatives: {canonical(record['alternatives'])}."
    )


def alternatives_are_scalar(record: dict[str, Any] | None) -> bool:
    if not record or record.get("alternatives_status") != "enumerated":
        return False
    values = record.get("alternatives") or []
    if not values:
        return False
    return all(not isinstance(item, (dict, list)) for item in values)


def value_outside_alternatives(record: dict[str, Any] | None, value: Any) -> bool:
    from .util import json_equal

    if not alternatives_are_scalar(record):
        return False
    return not any(json_equal(value, item) for item in record["alternatives"])

