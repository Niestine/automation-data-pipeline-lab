"""Denial checks for the constraint subset, plus BART-style repairability.

The subset is not-null, unique, primary key, one binary functional dependency,
one constant conditional rule, and a numeric range. A cell change is detectable
when the dirty table involves that cell in one of these violations. Repairability
is the share of the clean value in the candidate bag. A constant rule scores 1.
An open numeric range scores 0, and so do not-null, unique, and primary key.
Several rules keep the maximum.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import Hit, ViewRow
from .schema import FieldSpec, RuleSpec, Schema, pattern_error, render_value


def collect_hits(rows: list[ViewRow], schema: Schema) -> list[Hit]:
    hits: list[Hit] = []
    hits.extend(type_and_pattern_hits(rows, schema))
    hits.extend(structural_hits(rows, schema))
    return hits


def type_and_pattern_hits(rows: list[ViewRow], schema: Schema) -> list[Hit]:
    hits: list[Hit] = []
    for row in rows:
        if row.ragged:
            hits.append(
                _hit(row, schema.fields[0].name, "ragged", "type", "ragged row")
            )
        for spec in schema.fields:
            for message in row.cast_errors.get(spec.name, []):
                hits.append(_hit(row, spec.name, f"type:{spec.name}", "type", message))
            message = pattern_error(row.values.get(spec.name), spec)
            if message:
                hits.append(_hit(row, spec.name, f"pattern:{spec.name}", "pattern", message))
    return hits


def structural_hits(rows: list[ViewRow], schema: Schema) -> list[Hit]:
    hits: list[Hit] = []
    for spec in schema.fields:
        hits.extend(_required_hits(rows, schema, spec))
        hits.extend(_length_enum_hits(rows, spec))
        if spec.constraints.unique:
            hits.extend(_unique_hits(rows, spec.name, f"unique:{spec.name}"))
        if spec.constraints.minimum is not None or spec.constraints.maximum is not None:
            hits.extend(
                _range_hits(
                    rows,
                    spec.name,
                    f"range:{spec.name}",
                    spec.constraints.minimum,
                    spec.constraints.maximum,
                )
            )
    hits.extend(_primary_hits(rows, schema))
    for rule in schema.rules:
        hits.extend(_rule_hits(rows, rule))
    return hits


def denial_hits(rows: list[ViewRow], schema: Schema) -> list[Hit]:
    """Violations that make a cell change detectable in the BART sense."""

    return [
        hit
        for hit in structural_hits(rows, schema)
        if hit.kind in {"not_null", "unique", "primary_key", "fd", "constant", "range"}
    ]


def repairability(
    rows: list[ViewRow],
    schema: Schema,
    row_id: str,
    column: str,
    clean_value,
) -> tuple[bool, bool, str | None, list[str]]:
    """Return detectable, exactly_one, repairability text, and rule ids.

    Repairability is ``None`` when no denial rule sees the cell. Otherwise it
    is the maximum score across those rules, rendered as a decimal string.
    """

    hits = [
        hit
        for hit in denial_hits(rows, schema)
        if hit.row_id == row_id and hit.column == column
    ]
    rule_ids = sorted({hit.rule_id for hit in hits})
    if not rule_ids:
        return False, False, None, []
    scores: list[Decimal] = []
    for rule_id in rule_ids:
        rule_hits = [hit for hit in hits if hit.rule_id == rule_id]
        kind = rule_hits[0].kind
        if kind == "constant":
            scores.append(Decimal(1))
        elif kind == "range":
            scores.append(Decimal(0))
        elif kind == "fd":
            rule = next(item for item in schema.rules if item.id == rule_id)
            bag = _fd_bag(rows, rule, row_id, column)
            if not bag:
                scores.append(Decimal(0))
            else:
                scores.append(Decimal(bag.count(clean_value)) / Decimal(len(bag)))
        else:
            # Not-null, unique, and primary key carry no context bag that holds
            # the clean value in this subset, so a minimal repair cannot recover it.
            scores.append(Decimal(0))
    score = max(scores)
    return True, len(rule_ids) == 1, format(score, "f"), rule_ids


def _fd_bag(rows: list[ViewRow], rule: RuleSpec, row_id: str, column: str) -> list:
    if column not in {rule.determinant, rule.dependent}:
        return []
    parents = {row.row_id: row.row_id for row in rows}

    def find(item: str) -> str:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    def union(left: str, right: str) -> None:
        ra, rb = find(left), find(right)
        if ra == rb:
            return
        if ra < rb:
            parents[rb] = ra
        else:
            parents[ra] = rb

    eligible = [
        row
        for row in rows
        if not row.ragged
        and row.values.get(rule.determinant) is not None
        and row.values.get(rule.dependent) is not None
    ]
    for left_index, left in enumerate(eligible):
        for right in eligible[left_index + 1 :]:
            if left.values[rule.determinant] == right.values[rule.determinant] and (
                left.values[rule.dependent] != right.values[rule.dependent]
            ):
                union(left.row_id, right.row_id)
    if row_id not in parents:
        return []
    root = find(row_id)
    members = [row for row in eligible if find(row.row_id) == root]
    if len(members) < 2:
        return []
    return [row.values[column] for row in members]


def _required_hits(rows: list[ViewRow], schema: Schema, spec: FieldSpec) -> list[Hit]:
    if not schema.requires(spec):
        return []
    hits = []
    for row in rows:
        if row.ragged:
            continue
        if row.values.get(spec.name) is None and not row.cast_errors.get(spec.name):
            hits.append(_hit(row, spec.name, f"required:{spec.name}", "not_null", "required value is null"))
    return hits


def _length_enum_hits(rows: list[ViewRow], spec: FieldSpec) -> list[Hit]:
    hits = []
    for row in rows:
        value = row.values.get(spec.name)
        if not isinstance(value, str):
            continue
        if spec.constraints.min_length is not None and len(value) < spec.constraints.min_length:
            hits.append(
                _hit(row, spec.name, f"minLength:{spec.name}", "length", "shorter than minLength")
            )
        if spec.constraints.max_length is not None and len(value) > spec.constraints.max_length:
            hits.append(
                _hit(row, spec.name, f"maxLength:{spec.name}", "length", "longer than maxLength")
            )
        if spec.constraints.enum is not None and value not in spec.constraints.enum:
            hits.append(_hit(row, spec.name, f"enum:{spec.name}", "enum", "value is outside enum"))
    return hits


def _unique_hits(rows: list[ViewRow], column: str, rule_id: str) -> list[Hit]:
    groups: dict[object, list[ViewRow]] = defaultdict(list)
    for row in rows:
        if row.ragged:
            continue
        value = row.values.get(column)
        if value is None or row.cast_errors.get(column):
            continue
        groups[value].append(row)
    hits = []
    for value, group in groups.items():
        if len(group) < 2:
            continue
        rendered = render_value(value)
        for row in group:
            hits.append(_hit(row, column, rule_id, "unique", f"duplicate value {rendered}"))
    return hits


def _primary_hits(rows: list[ViewRow], schema: Schema) -> list[Hit]:
    groups: dict[tuple, list[ViewRow]] = defaultdict(list)
    for row in rows:
        if row.ragged or not row.row_id:
            continue
        key = tuple(row.values.get(name) for name in schema.primary_key)
        groups[key].append(row)
    hits = []
    for group in groups.values():
        if len(group) < 2:
            continue
        for row in group:
            hits.append(
                _hit(
                    row,
                    schema.primary_key[0],
                    "primary_key",
                    "primary_key",
                    f"duplicate primary key {row.row_id}",
                )
            )
    return hits


def _range_hits(
    rows: list[ViewRow],
    column: str,
    rule_id: str,
    minimum: Decimal | None,
    maximum: Decimal | None,
) -> list[Hit]:
    hits = []
    for row in rows:
        value = row.values.get(column)
        if not isinstance(value, Decimal):
            continue
        if minimum is not None and value < minimum:
            hits.append(_hit(row, column, rule_id, "range", f"{format(value, 'f')} below minimum"))
        elif maximum is not None and value > maximum:
            hits.append(_hit(row, column, rule_id, "range", f"{format(value, 'f')} above maximum"))
    return hits


def _rule_hits(rows: list[ViewRow], rule: RuleSpec) -> list[Hit]:
    if rule.kind == "fd":
        return _fd_hits(rows, rule)
    if rule.kind == "constant":
        return _constant_hits(rows, rule)
    if rule.kind == "range" and rule.column is not None:
        return _range_hits(rows, rule.column, rule.id, rule.minimum, rule.maximum)
    return []


def _fd_hits(rows: list[ViewRow], rule: RuleSpec) -> list[Hit]:
    grouped: dict[object, set] = defaultdict(set)
    for row in rows:
        if row.ragged:
            continue
        left = row.values.get(rule.determinant)
        right = row.values.get(rule.dependent)
        if left is None or right is None:
            continue
        grouped[left].add(right)
    conflicting = {key for key, values in grouped.items() if len(values) > 1}
    hits = []
    for row in rows:
        if row.ragged or row.values.get(rule.dependent) is None:
            continue
        left = row.values.get(rule.determinant)
        if left is None or left not in conflicting:
            continue
        rendered = sorted(render_value(value) for value in grouped[left])
        detail = f"{rule.determinant} {render_value(left)} has {rule.dependent} {'|'.join(rendered)}"
        hits.append(_hit(row, rule.determinant, rule.id, "fd", detail))
        hits.append(_hit(row, rule.dependent, rule.id, "fd", detail))
    return hits


def _constant_hits(rows: list[ViewRow], rule: RuleSpec) -> list[Hit]:
    hits = []
    for row in rows:
        if row.ragged:
            continue
        when_value = row.values.get(rule.when_field)
        then_value = row.values.get(rule.then_field)
        if when_value is None or then_value is None:
            continue
        if render_value(when_value) == rule.when_equals and render_value(then_value) != rule.then_equals:
            detail = (
                f"{rule.when_field}={rule.when_equals} implies "
                f"{rule.then_field}={rule.then_equals}"
            )
            hits.append(_hit(row, rule.when_field, rule.id, "constant", detail))
            hits.append(_hit(row, rule.then_field, rule.id, "constant", detail))
    return hits


def _hit(row: ViewRow, column: str, rule_id: str, kind: str, detail: str) -> Hit:
    return Hit(
        rule_id=rule_id,
        kind=kind,
        row_id=row.row_id,
        column=column,
        detail=detail,
        source_row=row.source_row,
        output_row=row.output_row,
    )
