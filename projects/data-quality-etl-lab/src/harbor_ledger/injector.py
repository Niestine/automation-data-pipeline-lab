"""Seeded greedy cell injector.

Optimal placement of detectable errors is NP-complete, so this pass tries a
finite list of candidate cells and commits a change only when it keeps every
earlier detectable error detectable. One cell is changed at most once. A quota
the pass cannot fill is a recorded shortfall, not an exception and not a loop.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from .errors import InjectionError
from .models import ViewRow
from .rules import denial_hits, repairability
from .schema import Schema, match_form, render_value


@dataclass
class InjectionResult:
    rows: list[ViewRow]
    oracle: list[dict]
    shortfall: int
    exit_code: int


def run_injection(rows: list[ViewRow], schema: Schema, config: dict) -> InjectionResult:
    try:
        result = inject(rows, schema, config)
    except InjectionError:
        return InjectionResult(rows=rows, oracle=[], shortfall=0, exit_code=2)
    return InjectionResult(rows=result[0], oracle=result[1], shortfall=result[2], exit_code=0)


def inject(
    rows: list[ViewRow], schema: Schema, config: dict
) -> tuple[list[ViewRow], list[dict], int]:
    if denial_hits(rows, schema):
        raise InjectionError("clean fixture already violates a denial rule")
    working = [row.copy() for row in rows]
    immutable = set(config.get("immutable") or [])
    quota = int(config["detectable_quota"])
    undetectable_quota = int(config.get("undetectable_quota") or 0)
    placed: list[dict] = []
    changed: set[tuple[str, str]] = set()
    for proposal in _proposals(working, schema, config):
        detectable_count = sum(1 for item in placed if item["detectable"])
        undetectable_count = sum(1 for item in placed if not item["detectable"])
        if detectable_count >= quota and undetectable_count >= undetectable_quota:
            break
        row_id = proposal["row_id"]
        column = proposal["column"]
        if column in immutable or (row_id, column) in changed:
            continue
        row = _row(working, row_id)
        if row is None or row.ragged:
            continue
        clean = row.values.get(column)
        dirty = proposal["value"]
        if render_value(clean) == render_value(dirty) and clean == dirty:
            continue
        row.values[column] = dirty
        if column in row.cast_errors:
            row.cast_errors.pop(column, None)
        if not _prior_still_detectable(working, schema, placed):
            row.values[column] = clean
            continue
        detected, exactly_one, score, rule_ids = repairability(
            working, schema, row_id, column, clean
        )
        if detected and detectable_count >= quota:
            row.values[column] = clean
            continue
        if not detected and undetectable_count >= undetectable_quota:
            row.values[column] = clean
            continue
        if not detected and not config.get("allow_undetectable", True):
            row.values[column] = clean
            continue
        changed.add((row_id, column))
        placed.append(
            {
                "class": proposal["class"],
                "clean": render_value(clean),
                "clean_obj": clean,
                "column": column,
                "detectable": detected,
                "dirty": render_value(dirty),
                "exactly_one": exactly_one,
                "repairability": score,
                "row_id": row_id,
                "rules": rule_ids,
                "slice": config.get("slices", {}).get(row_id, "eval"),
            }
        )
    final_oracle = []
    for item in placed:
        detected, exactly_one, score, rule_ids = repairability(
            working, schema, item["row_id"], item["column"], item["clean_obj"]
        )
        # Keep the clean text in the oracle. Recompute detectability on the final table.
        final_oracle.append(
            {
                "class": item["class"],
                "clean": item["clean"],
                "column": item["column"],
                "detectable": detected,
                "dirty": item["dirty"],
                "exactly_one": exactly_one,
                "repairability": score,
                "row_id": item["row_id"],
                "rules": rule_ids,
                "slice": item["slice"],
            }
        )
    detectable_count = sum(1 for item in final_oracle if item["detectable"])
    shortfall = max(0, quota - detectable_count)
    for row in working:
        for spec in schema.fields:
            row.match[spec.name] = match_form(row.values.get(spec.name), spec)
            row.raw[spec.name] = render_value(row.values.get(spec.name))
    return working, final_oracle, shortfall


def _proposals(rows: list[ViewRow], schema: Schema, config: dict) -> list[dict]:
    explicit = config.get("proposals")
    if explicit is not None:
        return list(explicit)
    rng = random.Random(int(config.get("seed") or 0))
    immutable = set(config.get("immutable") or [])
    classes = list(config.get("classes") or ["typo", "constraint_break", "outlier", "missing"])
    cells = [
        (row.row_id, spec.name, spec.type)
        for row in rows
        for spec in schema.fields
        if spec.name not in immutable and spec.name not in schema.primary_key and not row.ragged
    ]
    rng.shuffle(cells)
    proposals = []
    for row_id, column, field_type in cells:
        order = list(classes)
        rng.shuffle(order)
        row = _row(rows, row_id)
        for klass in order:
            value = _random_value(rng, klass, row, column, field_type, rows, schema)
            if value is _SKIP:
                continue
            proposals.append(
                {"row_id": row_id, "column": column, "value": value, "class": klass}
            )
            break
    return proposals


class _Skip:
    pass


_SKIP = _Skip()


def _random_value(rng, klass, row, column, field_type, rows, schema):
    current = row.values.get(column)
    if klass == "missing":
        return None
    if klass == "typo":
        if isinstance(current, str) and current:
            chars = list(current)
            index = rng.randrange(len(chars))
            chars[index] = "X" if chars[index] != "X" else "Y"
            return "".join(chars)
        if isinstance(current, Decimal):
            return current + Decimal(1)
        return _SKIP
    if klass == "duplicated_value":
        others = [
            other.values.get(column)
            for other in rows
            if other.row_id != row.row_id and other.values.get(column) is not None
        ]
        if not others:
            return _SKIP
        return rng.choice(others)
    if klass == "outlier":
        if not isinstance(current, Decimal):
            return _SKIP
        ceiling = _range_max(schema, column)
        return ceiling + Decimal(config_span(rng))
    if klass == "bogus":
        if field_type == "string":
            return "??"
        return _SKIP
    if klass == "constraint_break":
        if isinstance(current, Decimal):
            return _range_max(schema, column) + Decimal(50)
        if isinstance(current, str):
            return current + "X"
        return _SKIP
    return _SKIP


def config_span(rng) -> int:
    return 1000 + rng.randrange(10)


def _range_max(schema: Schema, column: str) -> Decimal:
    spec = schema.field_map()[column]
    if spec.constraints.maximum is not None:
        return spec.constraints.maximum
    for rule in schema.rules:
        if rule.kind == "range" and rule.column == column and rule.maximum is not None:
            return rule.maximum
    return Decimal(100)


def _prior_still_detectable(rows: list[ViewRow], schema: Schema, placed: list[dict]) -> bool:
    hits = {(hit.row_id, hit.column) for hit in denial_hits(rows, schema)}
    for item in placed:
        if item["detectable"] and (item["row_id"], item["column"]) not in hits:
            return False
    return True


def _row(rows: list[ViewRow], row_id: str) -> ViewRow | None:
    for row in rows:
        if row.row_id == row_id:
            return row
    return None
