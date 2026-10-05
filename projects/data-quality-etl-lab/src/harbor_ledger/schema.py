"""Table Schema subset: physical missing-values, then a logical cast.

``missingValues`` is matched on the physical string before any cast. Numbers
honor ``decimalChar``, ``groupChar``, and ``bareNumber``. When ``bareNumber``
is false, only a schema-listed prefix or suffix is stripped, and the stripped
text is recorded. Dates accept ISO-8601 or one ``strptime`` pattern. The
format name ``any`` is rejected so ``01/02/2020`` cannot change meaning between
runs. ``pattern`` is applied as a full-string match, not a Python search.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .csvio import Dialect
from .errors import HarborError, SchemaError
from .models import Atomic, ViewRow

_DEFAULT_TRUE = ["true", "True", "TRUE", "1"]
_DEFAULT_FALSE = ["false", "False", "FALSE", "0"]
_TYPES = {"string", "number", "integer", "boolean", "date", "time", "datetime"}
_FIELD_KEYS = {
    "name",
    "type",
    "identifier_fold",
    "constraints",
    "bareNumber",
    "groupChar",
    "decimalChar",
    "affixes",
    "format",
    "trueValues",
    "falseValues",
}
_CONSTRAINT_KEYS = {
    "required",
    "unique",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "pattern",
    "enum",
}


@dataclass
class FieldConstraints:
    required: bool = False
    unique: bool = False
    min_length: int | None = None
    max_length: int | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    pattern: str | None = None
    enum: list[str] | None = None


@dataclass
class Affixes:
    prefixes: list[str] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)


@dataclass
class FieldSpec:
    name: str
    type: str
    identifier_fold: bool = False
    constraints: FieldConstraints = field(default_factory=FieldConstraints)
    bare_number: bool = True
    group_char: str | None = None
    decimal_char: str = "."
    affixes: Affixes = field(default_factory=Affixes)
    format: str | None = None
    true_values: list[str] = field(default_factory=lambda: list(_DEFAULT_TRUE))
    false_values: list[str] = field(default_factory=lambda: list(_DEFAULT_FALSE))


@dataclass
class RuleSpec:
    id: str
    kind: str
    determinant: str | None = None
    dependent: str | None = None
    when_field: str | None = None
    when_equals: str | None = None
    then_field: str | None = None
    then_equals: str | None = None
    column: str | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None


@dataclass
class Schema:
    name: str
    fields: list[FieldSpec]
    primary_key: list[str]
    missing_values: list[str]
    dialect: Dialect
    rules: list[RuleSpec]

    def field_map(self) -> dict[str, FieldSpec]:
        return {item.name: item for item in self.fields}

    def requires(self, spec: FieldSpec) -> bool:
        return spec.constraints.required or spec.name in self.primary_key


def load_schema(path: str | Path) -> Schema:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SchemaError("schema must be an object")
    return schema_from_dict(payload)


def schema_from_dict(payload: dict) -> Schema:
    _reject_unknown(payload, {"name", "fields", "primaryKey", "missingValues", "dialect", "rules"}, "schema")
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise SchemaError("schema name is required")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaError("schema fields must be a non-empty list")
    fields = [_field_from_dict(item) for item in raw_fields]
    names = [item.name for item in fields]
    if len(names) != len(set(names)):
        raise SchemaError("duplicate field name")
    primary = payload.get("primaryKey")
    if isinstance(primary, str):
        primary = [primary]
    if not isinstance(primary, list) or not primary or not all(isinstance(item, str) for item in primary):
        raise SchemaError("primaryKey must name at least one field")
    if any(item not in names for item in primary):
        raise SchemaError("primaryKey names an unknown field")
    missing = payload.get("missingValues", [""])
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        raise SchemaError("missingValues must be a list of strings")
    dialect = _dialect_from_dict(payload.get("dialect") or {})
    rules = [_rule_from_dict(item, names) for item in payload.get("rules") or []]
    return Schema(
        name=name,
        fields=fields,
        primary_key=list(primary),
        missing_values=list(missing),
        dialect=dialect,
        rules=rules,
    )


def cast_table(raw_rows: list, schema: Schema) -> list[ViewRow]:
    """Cast parsed data rows. ``raw_rows`` are ``RawRecord`` instances."""

    views: list[ViewRow] = []
    for raw in raw_rows:
        views.append(_cast_row(raw, schema))
    return views


def render_value(value: Atomic) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def match_form(value: Atomic, spec: FieldSpec) -> str | None:
    """String used by blocking and field agreement. Identifier columns use NFKC."""

    if value is None:
        return None
    text = value if isinstance(value, str) else render_value(value)
    if spec.identifier_fold:
        text = unicodedata.normalize("NFKC", text)
    return standardize_text(text)


def standardize_text(text: str) -> str:
    """Casefold, strip punctuation, and expand a small abbreviation table.

    The duplicate survey's ETL step: without a standard form, ``44 W. 4th St.``
    and ``44 West Fourth Street`` compare as different strings. Atoms are runs
    of Unicode letters and digits, so accented and non-Latin text is kept.
    """

    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    atoms = re.findall(r"[^\W_]+", folded)
    return " ".join(_ABBREVIATIONS.get(atom, atom) for atom in atoms)


_ABBREVIATIONS = {
    "w": "west",
    "st": "street",
    "4th": "fourth",
    "blk": "black",
    "nvy": "navy",
    "gry": "grey",
}


def pattern_error(value: Atomic, spec: FieldSpec) -> str | None:
    pattern = spec.constraints.pattern
    if pattern is None or not isinstance(value, str):
        return None
    try:
        matched = re.fullmatch(pattern, value) is not None
    except re.error as exc:
        raise SchemaError(f"pattern on {spec.name} is not a usable expression") from exc
    if matched:
        return None
    return f"pattern {pattern} rejected {value!r}"


def _cast_row(raw, schema: Schema) -> ViewRow:
    values: dict[str, Atomic] = {}
    raw_map: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    cast_errors: dict[str, list[str]] = {}
    width = len(schema.fields)
    for position, spec in enumerate(schema.fields):
        if position < len(raw.fields):
            physical = raw.fields[position]
        else:
            physical = ""
            cast_errors.setdefault(spec.name, []).append("ragged-missing")
        raw_map[spec.name] = physical
        for trim in raw.trims:
            if trim["index"] == position:
                notes.setdefault(spec.name, []).append(
                    f"trim {trim['before']!r} -> {trim['after']!r}"
                )
        if physical in schema.missing_values and "ragged-missing" not in cast_errors.get(spec.name, []):
            values[spec.name] = None
            continue
        if "ragged-missing" in cast_errors.get(spec.name, []):
            values[spec.name] = None
            continue
        try:
            semantic, stripped = _cast_physical(physical, spec)
        except _CastFailure as exc:
            values[spec.name] = None
            cast_errors.setdefault(spec.name, []).append(str(exc))
            continue
        values[spec.name] = semantic
        if stripped:
            notes.setdefault(spec.name, []).append("stripped " + " ".join(stripped))
    if len(raw.fields) > width:
        for position in range(width, len(raw.fields)):
            cast_errors.setdefault(schema.fields[-1].name, []).append(
                f"ragged-extra {raw.fields[position]!r}"
            )
    key_parts: list[str] = []
    key_ok = True
    for name in schema.primary_key:
        value = values.get(name)
        if value is None:
            key_ok = False
            break
        key_parts.append(render_value(value))
    row_id = "|".join(key_parts) if key_ok else ""
    match = {
        spec.name: match_form(values.get(spec.name), spec)
        for spec in schema.fields
    }
    return ViewRow(
        row_id=row_id,
        source_row=raw.source_row,
        output_row=raw.output_row or 0,
        values=values,
        raw=raw_map,
        notes=notes,
        cast_errors=cast_errors,
        match=match,
        ragged=raw.kind == "ragged" or bool(raw.parse_errors),
        parse_errors=list(raw.parse_errors),
    )


class _CastFailure(Exception):
    pass


def _cast_physical(physical: str, spec: FieldSpec) -> tuple[Atomic, list[str]]:
    if spec.type == "string":
        text = unicodedata.normalize("NFC", physical)
        return text, []
    if spec.type == "boolean":
        if physical in spec.true_values:
            return True, []
        if physical in spec.false_values:
            return False, []
        raise _CastFailure(f"boolean token {physical!r} is not in the token lists")
    if spec.type in {"number", "integer"}:
        return _cast_number(physical, spec)
    if spec.type == "date":
        return _cast_date(physical, spec), []
    if spec.type == "time":
        return _cast_time(physical, spec), []
    if spec.type == "datetime":
        return _cast_datetime(physical, spec), []
    raise SchemaError(f"unsupported type {spec.type}")


def _cast_number(physical: str, spec: FieldSpec) -> tuple[Decimal, list[str]]:
    text = physical
    stripped: list[str] = []
    if not spec.bare_number:
        for prefix in spec.affixes.prefixes:
            if prefix and text.startswith(prefix):
                text = text[len(prefix) :]
                stripped.append(prefix)
                break
        for suffix in spec.affixes.suffixes:
            if suffix and text.endswith(suffix):
                text = text[: -len(suffix)]
                stripped.append(suffix)
                break
    if spec.group_char:
        text = text.replace(spec.group_char, "")
    if spec.decimal_char != ".":
        text = text.replace(spec.decimal_char, ".")
    if spec.type == "integer":
        if not re.fullmatch(r"[+-]?\d+", text):
            raise _CastFailure(f"integer {physical!r} did not parse")
    elif not re.fullmatch(r"[+-]?(?:\d+)(?:\.\d+)?", text):
        raise _CastFailure(f"number {physical!r} did not parse")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise _CastFailure(f"number {physical!r} did not parse") from exc
    return value, stripped


def _cast_date(physical: str, spec: FieldSpec) -> date:
    fmt = spec.format or "YYYY-MM-DD"
    if fmt == "any":
        raise SchemaError("format any is rejected")
    if fmt in {"YYYY-MM-DD", "iso"}:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", physical):
            raise _CastFailure(f"date {physical!r} is not YYYY-MM-DD")
        try:
            return datetime.strptime(physical, "%Y-%m-%d").date()
        except ValueError as exc:
            raise _CastFailure(f"date {physical!r} is not a calendar day") from exc
    if "%" not in fmt:
        raise SchemaError(f"date format {fmt!r} is not ISO-8601 or a strptime pattern")
    try:
        return datetime.strptime(physical, fmt).date()
    except ValueError as exc:
        raise _CastFailure(f"date {physical!r} does not match {fmt}") from exc


def _cast_time(physical: str, spec: FieldSpec) -> time:
    fmt = spec.format or "HH:MM:SS"
    pattern = "%H:%M:%S" if fmt in {"HH:MM:SS", "iso"} else fmt
    if "%" not in pattern and fmt not in {"HH:MM:SS", "iso"}:
        raise SchemaError(f"time format {fmt!r} is not supported")
    if fmt in {"HH:MM:SS", "iso"} and not re.fullmatch(r"\d{2}:\d{2}:\d{2}", physical):
        raise _CastFailure(f"time {physical!r} is not HH:MM:SS")
    try:
        return datetime.strptime(physical, pattern).time()
    except ValueError as exc:
        raise _CastFailure(f"time {physical!r} did not parse") from exc


def _cast_datetime(physical: str, spec: FieldSpec) -> datetime:
    fmt = spec.format or "YYYY-MM-DDTHH:MM:SS"
    if fmt in {"YYYY-MM-DDTHH:MM:SS", "iso"}:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", physical):
            raise _CastFailure(f"datetime {physical!r} is not ISO-8601")
        try:
            return datetime.strptime(physical, "%Y-%m-%dT%H:%M:%S")
        except ValueError as exc:
            raise _CastFailure(f"datetime {physical!r} did not parse") from exc
    if "%" not in fmt:
        raise SchemaError(f"datetime format {fmt!r} is not supported")
    try:
        return datetime.strptime(physical, fmt)
    except ValueError as exc:
        raise _CastFailure(f"datetime {physical!r} did not parse") from exc


def _field_from_dict(payload: dict) -> FieldSpec:
    if not isinstance(payload, dict):
        raise SchemaError("field must be an object")
    _reject_unknown(payload, _FIELD_KEYS, "field")
    name = payload.get("name")
    kind = payload.get("type", "string")
    if not isinstance(name, str) or not name:
        raise SchemaError("field name is required")
    if kind not in _TYPES:
        raise SchemaError(f"field {name} has unsupported type {kind!r}")
    fmt = payload.get("format")
    if fmt == "any":
        raise SchemaError(f"field {name} uses format any, which this lab rejects")
    if fmt is not None and not isinstance(fmt, str):
        raise SchemaError(f"field {name} format must be a string")
    constraints = _constraints_from_dict(payload.get("constraints") or {}, name)
    affixes_raw = payload.get("affixes") or {}
    if not isinstance(affixes_raw, dict):
        raise SchemaError(f"field {name} affixes must be an object")
    _reject_unknown(affixes_raw, {"prefixes", "suffixes"}, f"{name} affixes")
    prefixes = affixes_raw.get("prefixes") or []
    suffixes = affixes_raw.get("suffixes") or []
    if not all(isinstance(item, str) for item in prefixes + suffixes):
        raise SchemaError(f"field {name} affixes must be strings")
    true_values = payload.get("trueValues", list(_DEFAULT_TRUE))
    false_values = payload.get("falseValues", list(_DEFAULT_FALSE))
    if not all(isinstance(item, str) for item in true_values + false_values):
        raise SchemaError(f"field {name} boolean tokens must be strings")
    group = payload.get("groupChar")
    if group is not None and not isinstance(group, str):
        raise SchemaError(f"field {name} groupChar must be a string")
    decimal_char = payload.get("decimalChar", ".")
    if not isinstance(decimal_char, str) or not decimal_char:
        raise SchemaError(f"field {name} decimalChar must be a string")
    return FieldSpec(
        name=name,
        type=kind,
        identifier_fold=bool(payload.get("identifier_fold", False)),
        constraints=constraints,
        bare_number=bool(payload.get("bareNumber", True)),
        group_char=group,
        decimal_char=decimal_char,
        affixes=Affixes(prefixes=list(prefixes), suffixes=list(suffixes)),
        format=fmt,
        true_values=list(true_values),
        false_values=list(false_values),
    )


def _constraints_from_dict(payload: dict, name: str) -> FieldConstraints:
    if not isinstance(payload, dict):
        raise SchemaError(f"constraints on {name} must be an object")
    _reject_unknown(payload, _CONSTRAINT_KEYS, f"{name} constraints")
    enum = payload.get("enum")
    if enum is not None and (not isinstance(enum, list) or not all(isinstance(item, str) for item in enum)):
        raise SchemaError(f"enum on {name} must be a list of strings")
    pattern = payload.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        raise SchemaError(f"pattern on {name} must be a string")
    return FieldConstraints(
        required=bool(payload.get("required", False)),
        unique=bool(payload.get("unique", False)),
        min_length=_optional_int(payload.get("minLength"), f"{name} minLength"),
        max_length=_optional_int(payload.get("maxLength"), f"{name} maxLength"),
        minimum=_optional_decimal(payload.get("minimum"), f"{name} minimum"),
        maximum=_optional_decimal(payload.get("maximum"), f"{name} maximum"),
        pattern=pattern,
        enum=list(enum) if enum is not None else None,
    )


def _rule_from_dict(payload: dict, names: list[str]) -> RuleSpec:
    if not isinstance(payload, dict):
        raise SchemaError("rule must be an object")
    kind = payload.get("kind")
    rule_id = payload.get("id")
    if not isinstance(rule_id, str) or not isinstance(kind, str):
        raise SchemaError("rule id and kind are required")
    if kind == "fd":
        determinant = payload.get("determinant")
        dependent = payload.get("dependent")
        if determinant not in names or dependent not in names:
            raise SchemaError(f"rule {rule_id} names an unknown field")
        return RuleSpec(id=rule_id, kind="fd", determinant=determinant, dependent=dependent)
    if kind == "constant":
        when = payload.get("when") or {}
        then = payload.get("then") or {}
        if when.get("field") not in names or then.get("field") not in names:
            raise SchemaError(f"rule {rule_id} names an unknown field")
        if not isinstance(when.get("equals"), str) or not isinstance(then.get("equals"), str):
            raise SchemaError(f"rule {rule_id} equals must be a string")
        return RuleSpec(
            id=rule_id,
            kind="constant",
            when_field=when["field"],
            when_equals=when["equals"],
            then_field=then["field"],
            then_equals=then["equals"],
        )
    if kind == "range":
        column = payload.get("column")
        if column not in names:
            raise SchemaError(f"rule {rule_id} names an unknown field")
        return RuleSpec(
            id=rule_id,
            kind="range",
            column=column,
            minimum=_optional_decimal(payload.get("minimum"), f"{rule_id} minimum"),
            maximum=_optional_decimal(payload.get("maximum"), f"{rule_id} maximum"),
        )
    raise SchemaError(f"rule {rule_id} has unsupported kind {kind!r}")


def _dialect_from_dict(payload: dict) -> Dialect:
    if not isinstance(payload, dict):
        raise SchemaError("dialect must be an object")
    _reject_unknown(
        payload,
        {"delimiter", "quote", "header", "skip_rows", "skip_blank_rows", "comment_prefix", "trim", "encoding"},
        "dialect",
    )
    header = payload.get("header", True)
    skip_blank_rows = payload.get("skip_blank_rows", True)
    trim = payload.get("trim", False)
    if not all(isinstance(item, bool) for item in (header, skip_blank_rows, trim)):
        raise SchemaError("dialect header, skip_blank_rows, and trim must be booleans")
    skip_rows = payload.get("skip_rows", 0)
    if isinstance(skip_rows, bool) or not isinstance(skip_rows, int):
        raise SchemaError("dialect skip_rows must be an integer")
    texts = {key: payload.get(key, default) for key, default in (("delimiter", ","), ("quote", '"'), ("encoding", "utf-8"))}
    comment_prefix = payload.get("comment_prefix")
    if not all(isinstance(item, str) for item in texts.values()) or (
        comment_prefix is not None and not isinstance(comment_prefix, str)
    ):
        raise SchemaError("dialect delimiter, quote, comment_prefix, and encoding must be strings")
    try:
        return Dialect(
            header=header,
            skip_rows=skip_rows,
            skip_blank_rows=skip_blank_rows,
            comment_prefix=comment_prefix or None,
            trim=trim,
            **texts,
        )
    except HarborError as exc:
        raise SchemaError(f"dialect is invalid: {exc}") from exc


def _optional_int(value, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{label} must be an integer")
    return value


def _optional_decimal(value, label: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SchemaError(f"{label} must be an integer or a decimal string")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise SchemaError(f"{label} is not a decimal") from exc


def _reject_unknown(payload: dict, allowed: set[str], label: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SchemaError(f"{label} has unknown keys: {', '.join(unknown)}")
