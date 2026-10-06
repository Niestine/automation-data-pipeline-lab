"""Load the regression corpus and compare it to the strict and legacy parsers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .decisions import DECISIONS
from .model import ParseFailure, ParseSuccess, RepairEvent
from .recognize import hardened_parse, legacy_parse
from .unparse import unparse

BUCKETS = {"y", "n", "i", "c", "b"}


@dataclass(frozen=True)
class Case:
    name: str
    data: bytes
    meta: dict


def load_corpus(path: Path) -> list[Case]:
    cases: list[Case] = []
    for blob in sorted(path.glob("*.bin")):
        sidecar = blob.with_suffix(".json")
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        cases.append(Case(blob.name, blob.read_bytes(), meta))
    return cases


def catalog_problems(cases: list[Case]) -> list[str]:
    problems: list[str] = []
    if not cases:
        return ["corpus is empty"]
    seen: dict[tuple, list[str]] = {}
    covered: set[str] = set()
    for case in cases:
        meta = case.meta
        name = case.name
        bucket = meta.get("bucket")
        decision_id = meta.get("decision_id")
        if bucket not in BUCKETS:
            problems.append(f"{name} has bucket {bucket!r}")
            continue
        if decision_id not in DECISIONS:
            problems.append(f"{name} has unknown decision {decision_id!r}")
            continue
        covered.add(decision_id)
        if meta.get("header") not in {"present", "absent"}:
            problems.append(f"{name} header policy is missing")
            continue
        if bucket in {"y", "n", "i"} and meta.get("decode_policy") != "fatal":
            problems.append(f"{name} strict decode policy is not fatal")
        if bucket == "c":
            if meta.get("agreement") is not True or meta.get("conformance_oracle") is not False:
                problems.append(f"{name} agreement sidecar is not marked as a non-oracle")
        if bucket == "b":
            row = DECISIONS[decision_id]
            if meta.get("removal_condition") != row.removal_condition:
                problems.append(f"{name} removal condition drifted from {decision_id}")
            if meta.get("end_state") != row.end_state:
                problems.append(f"{name} end state drifted from {decision_id}")
            if meta.get("break_date") != row.break_date:
                problems.append(f"{name} break date drifted from {decision_id}")
            if not isinstance(meta.get("legacy"), dict) or not isinstance(meta.get("hardened"), dict):
                problems.append(f"{name} is missing a legacy or hardened block")
                continue
        for event_id in _sidecar_event_ids(meta):
            if event_id not in DECISIONS:
                problems.append(f"{name} records a repair event with unknown decision {event_id!r}")
        key = _signature(case)
        for other in seen.get(key, []):
            other_data = _data_of(other, cases)
            if _proper_prefix(case.data, other_data):
                problems.append(f"{name} repeats the signature of {other} as a proper prefix")
            elif _proper_prefix(other_data, case.data):
                problems.append(f"{other} repeats the signature of {name} as a proper prefix")
        seen.setdefault(key, []).append(name)
    missing = sorted(set(DECISIONS) - covered)
    for decision_id in missing:
        problems.append(f"{decision_id} has no corpus file")
    for row in DECISIONS.values():
        if row.legacy == "repair" and not (row.removal_condition and row.end_state and row.break_date):
            problems.append(f"{row.decision_id} repair row is missing an end state or removal condition")
    return problems


def case_problems(case: Case) -> list[str]:
    meta = case.meta
    problems: list[str] = []
    strict_expected = strict_expectation(case)
    strict = _call(case, "hardened", None)
    legacy_result = _call(case, "legacy", None)
    for result in (strict, legacy_result):
        for event in result.events:
            if event.decision_id not in DECISIONS:
                problems.append(f"{case.name} emitted a repair event with unknown decision {event.decision_id!r}")
    if not _match(strict, strict_expected):
        problems.append(f"{case.name} strict outcome { _dump(strict) } != {strict_expected}")
    if isinstance(strict, ParseSuccess):
        if strict.events or strict.bom_stripped:
            problems.append(f"{case.name} strict success carried a repair or a BOM strip")
        problems.extend(_round_trip(case, strict))
    elif isinstance(strict, ParseFailure):
        if strict.events or strict.bom_stripped:
            problems.append(f"{case.name} strict failure carried a repair or a BOM strip")
    legacy_expected = legacy_expectation(case)
    if meta["bucket"] != "b" and legacy_expected is not None:
        if not _match(legacy_result, legacy_expected):
            problems.append(f"{case.name} legacy drifted from the strict records")
    if meta["bucket"] == "b":
        if not _match(legacy_result, meta["legacy"]):
            problems.append(f"{case.name} legacy outcome { _dump(legacy_result) } != {meta['legacy']}")
        if _same_acceptance(strict, legacy_result) and _records_of(strict) == _records_of(legacy_result):
            problems.append(f"{case.name} was marked as a break but the profiles agree")
    return problems


def strict_expectation(case: Case) -> dict:
    if case.meta["bucket"] == "b":
        return case.meta["hardened"]
    return {
        "error_code": case.meta.get("error_code"),
        "decision_id": case.meta.get("decision_id"),
        "record_index": case.meta.get("record_index"),
        "field_index": case.meta.get("field_index"),
        "byte_offset": case.meta.get("byte_offset"),
        "records": case.meta.get("records"),
        "header_values": case.meta.get("header_values"),
        "events": case.meta.get("events") or [],
        "bom_stripped": case.meta.get("bom_stripped", False),
    }


def legacy_expectation(case: Case) -> dict | None:
    if case.meta["bucket"] == "b":
        return case.meta["legacy"]
    if case.meta["bucket"] in {"y", "c"} or (
        case.meta["bucket"] == "i" and case.meta.get("records") is not None
    ):
        return {
            "error_code": None,
            "records": case.meta["records"],
            "header_values": case.meta.get("header_values"),
            "events": [],
            "bom_stripped": False,
        }
    return None


def outcome_matches(result: ParseSuccess | ParseFailure, expected: dict) -> bool:
    return _match(result, expected)


def call_case(case: Case, profile: str, mutant: str | None) -> ParseSuccess | ParseFailure:
    return _call(case, profile, mutant)


def _call(case: Case, profile: str, mutant: str | None) -> ParseSuccess | ParseFailure:
    fn = hardened_parse if profile == "hardened" else legacy_parse
    return fn(
        case.data,
        header=case.meta["header"],
        charset=case.meta.get("charset", "utf-8"),
        field_limit=int(case.meta.get("field_limit", 4096)),
        mutant=mutant,
    )


def _match(result: ParseSuccess | ParseFailure, expected: dict) -> bool:
    events = _events(expected.get("events") or [])
    bom = bool(expected.get("bom_stripped", False))
    if expected.get("error_code"):
        if not isinstance(result, ParseFailure):
            return False
        return (
            result.code == expected["error_code"]
            and result.decision_id == expected.get("decision_id", result.decision_id)
            and result.record_index == expected.get("record_index")
            and result.field_index == expected.get("field_index")
            and result.byte_offset == expected.get("byte_offset")
            and result.bom_stripped is bom
            and result.events == events
        )
    if not isinstance(result, ParseSuccess):
        return False
    return (
        result.records == _rows(expected.get("records"))
        and result.header == _header(expected.get("header_values"))
        and result.bom_stripped is bom
        and result.events == events
    )


def _round_trip(case: Case, result: ParseSuccess) -> list[str]:
    try:
        emitted = unparse(result.header, result.records)
    except Exception as exc:  # noqa: BLE001 - the corpus reports the emitter failure
        return [f"{case.name} unparse raised {type(exc).__name__}"]
    again = hardened_parse(
        emitted,
        header=case.meta["header"],
        charset="utf-8",
        field_limit=int(case.meta.get("field_limit", 4096)),
    )
    if not isinstance(again, ParseSuccess):
        return [f"{case.name} round trip rejected the strict emission"]
    if again.records != result.records or again.header != result.header or again.events:
        return [f"{case.name} round trip changed the records or added a repair"]
    return []


def _events(items: list[dict]) -> tuple[RepairEvent, ...]:
    return tuple(
        RepairEvent(
            decision_id=item["decision_id"],
            record_index=item["record_index"],
            field_index=item["field_index"],
            byte_offset=item["byte_offset"],
        )
        for item in items
    )


def _rows(value: list | None) -> tuple[tuple[str, ...], ...]:
    if not value:
        return ()
    return tuple(tuple(row) for row in value)


def _header(value: list | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(value)


def _sidecar_event_ids(meta: dict) -> list[str]:
    blocks = [meta]
    if meta.get("bucket") == "b":
        blocks += [meta["legacy"], meta["hardened"]]
    return [item.get("decision_id") for block in blocks for item in block.get("events") or []]


def _signature(case: Case) -> tuple:
    meta = case.meta
    if meta["bucket"] == "b":
        block = meta["hardened"]
        outcome = (block.get("error_code"), _canon(block.get("records")), _canon(block.get("header_values")))
    else:
        outcome = (meta.get("error_code"), _canon(meta.get("records")), _canon(meta.get("header_values")))
    return (meta["bucket"], meta["decision_id"], meta["header"], outcome)


def _canon(value):
    if value is None:
        return None
    return tuple(tuple(row) for row in value)


def _data_of(name: str, cases: list[Case]) -> bytes:
    for case in cases:
        if case.name == name:
            return case.data
    return b""


def _proper_prefix(left: bytes, right: bytes) -> bool:
    return len(left) < len(right) and right.startswith(left)


def _same_acceptance(left: ParseSuccess | ParseFailure, right: ParseSuccess | ParseFailure) -> bool:
    return isinstance(left, ParseSuccess) is isinstance(right, ParseSuccess)


def _records_of(result: ParseSuccess | ParseFailure):
    if isinstance(result, ParseSuccess):
        return (result.header, result.records)
    return None


def _dump(result: ParseSuccess | ParseFailure) -> str:
    if isinstance(result, ParseFailure):
        return (
            f"failure {result.code} {result.decision_id} "
            f"record={result.record_index} field={result.field_index} byte={result.byte_offset} "
            f"bom={result.bom_stripped} events={result.events}"
        )
    return f"success header={result.header} records={result.records} bom={result.bom_stripped} events={result.events}"
