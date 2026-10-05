"""Four read-only detectors. They never repair the snapshot they score.

The published finding set is the union. A cell may carry a pattern error and a
constraint violation at the same time. Min-k agreement is implemented so a test
can show that it drops singleton hits; the pipeline does not call it.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median

from .dedup import PairDecision, match_rows
from .models import ViewRow
from .rules import structural_hits, type_and_pattern_hits
from .schema import Schema

_KIND_CLASS = {
    "type": "bogus",
    "pattern": "bogus",
    "not_null": "missing",
    "unique": "constraint_break",
    "primary_key": "constraint_break",
    "fd": "constraint_break",
    "constant": "constraint_break",
    "range": "constraint_break",
    "enum": "constraint_break",
    "length": "constraint_break",
}


def run_detectors(
    rows: list[ViewRow],
    schema: Schema,
    profile: dict,
    order: list[str] | None = None,
    repair_between: bool = False,
) -> list[dict]:
    sequence = list(order or profile["detector_order"])
    snapshot = [row.copy() for row in rows]
    findings: list[dict] = []
    for name in sequence:
        found = DETECTORS[name](snapshot, schema, profile)
        findings.extend(found)
        if repair_between:
            snapshot = _blank_bogus(snapshot, found)
    return findings


def pattern_type_detector(rows: list[ViewRow], schema: Schema, profile: dict) -> list[dict]:
    return [_from_hit(hit) for hit in type_and_pattern_hits(rows, schema)]


def constraint_detector(rows: list[ViewRow], schema: Schema, profile: dict) -> list[dict]:
    return [_from_hit(hit) for hit in structural_hits(rows, schema)]


def outlier_detector(rows: list[ViewRow], schema: Schema, profile: dict) -> list[dict]:
    spec = profile["outlier"]
    threshold = Decimal(spec["threshold"])
    minimum_rows = int(spec.get("minimum_rows", 5))
    findings = []
    for field in schema.fields:
        if field.type not in {"number", "integer"}:
            continue
        indexed = [
            (row, row.values.get(field.name))
            for row in rows
            if isinstance(row.values.get(field.name), Decimal) and not row.ragged
        ]
        if len(indexed) < minimum_rows:
            continue
        flags = modified_z_flags([value for _row, value in indexed], threshold)
        for (row, value), flagged in zip(indexed, flags):
            if not flagged:
                continue
            findings.append(
                _finding(
                    row,
                    field.name,
                    "outlier",
                    "outlier",
                    "modified_z",
                    f"{format(value, 'f')} is a rare deviation",
                )
            )
    return findings


def duplicate_detector(rows: list[ViewRow], schema: Schema, profile: dict) -> list[dict]:
    comparable = [row for row in rows if row.row_id and not row.ragged and not row.cast_errors]
    links, _review, _clusters = match_rows(comparable, profile)
    return link_findings(links, comparable)


def link_findings(links: list[PairDecision], rows: list[ViewRow]) -> list[dict]:
    by_id = {row.row_id: row for row in rows}
    findings = []
    for link in links:
        for row_id, source in ((link.left, link.left_source), (link.right, link.right_source)):
            row = by_id.get(row_id)
            if row is None:
                continue
            other = link.right if row_id == link.left else link.left
            findings.append(
                _finding(
                    row,
                    "title",
                    "duplicate",
                    "duplicated_value",
                    "link",
                    f"link with {other} weight {format(link.weight.quantize(Decimal('0.000001')), 'f')}",
                )
            )
    return findings


def modified_z_flags(values: list[Decimal], threshold: Decimal) -> list[bool]:
    if not values:
        return []
    center = Decimal(str(median(values)))
    deviations = [abs(value - center) for value in values]
    mad = Decimal(str(median(deviations)))
    if mad == 0:
        return [value != center for value in values]
    flags = []
    for value in values:
        score = Decimal("0.6745") * abs(value - center) / mad
        flags.append(score > threshold)
    return flags


def min_k_filter(findings: list[dict], k: int) -> list[dict]:
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for finding in findings:
        grouped[(finding["record_key"], finding["column"])].add(finding["detector"])
    kept = {cell for cell, detectors in grouped.items() if len(detectors) >= k}
    return [
        finding
        for finding in findings
        if (finding["record_key"], finding["column"]) in kept
    ]


def finding_signature(finding: dict) -> tuple:
    return (
        finding["source_row"],
        finding["column"],
        finding["detector"],
        finding["class"],
        finding["rule"],
        finding["record_key"],
        finding["detail"],
    )


def _blank_bogus(rows: list[ViewRow], findings: list[dict]) -> list[ViewRow]:
    """A separate, manifest-flagged path. The published run does not call this."""

    copies = [row.copy() for row in rows]
    index = {(row.row_id, row.source_row): row for row in copies}
    for finding in findings:
        if finding["class"] != "bogus":
            continue
        row = index.get((finding["record_key"], finding["source_row"]))
        if row is None:
            continue
        column = finding["column"]
        row.values[column] = None
        row.cast_errors.pop(column, None)
        row.raw[column] = ""
        if column in row.match:
            row.match[column] = None
    return copies


def _from_hit(hit) -> dict:
    row_stub = ViewRow(
        row_id=hit.row_id,
        source_row=hit.source_row,
        output_row=hit.output_row,
        values={},
        raw={},
    )
    return _finding(
        row_stub,
        hit.column,
        "pattern_type" if hit.kind in {"type", "pattern"} else "constraint",
        _KIND_CLASS[hit.kind],
        hit.rule_id,
        hit.detail,
    )


def _finding(row: ViewRow, column: str, detector: str, klass: str, rule: str, detail: str) -> dict:
    return {
        "class": klass,
        "column": column,
        "detail": detail,
        "detector": detector,
        "output_row": row.output_row,
        "record_key": row.row_id,
        "rule": rule,
        "source_row": row.source_row,
    }


DETECTORS = {
    "pattern_type": pattern_type_detector,
    "constraint": constraint_detector,
    "outlier": outlier_detector,
    "duplicate": duplicate_detector,
}
