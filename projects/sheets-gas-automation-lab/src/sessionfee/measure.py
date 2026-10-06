"""Run the local baselines and record what this package actually did.

The counts below are produced by executing the lab. They are not the
published rates from the spreadsheet papers or the Sheets quota tables.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sessionfee.canonical import SessionInput, fingerprint, user_entered_stub
from sessionfee.dateserial import leap_year_bug_serial, to_serial
from sessionfee.dimensions import (
    DIMENSIONLESS,
    HOUR,
    JPY,
    JPY_PER_HOUR,
    DimensionMismatch,
    Quantity,
    UndeclaredDimension,
    UnsupportedConversion,
    add,
    compare,
    dimension_for_column,
    fahrenheit_to_celsius,
    multiply,
    numeric_add,
)
from sessionfee.fixtures import decimal_field, load_json, rows_from_document, sessions_from_document
from sessionfee.formula_budget import LOCAL_CHAIN_CEILING, evaluate_chain, evaluate_formula
from sessionfee.idempotency import AppendBaseline, ContentHashBaseline, Ledger
from sessionfee.invariants import check_invariant
from sessionfee.plan import (
    BatchPlan,
    CellWrite,
    MemorySheet,
    UpdateRequest,
    apply_separate,
    commit_batch,
)
from sessionfee.schema import check_rows, dimensions_consistent
from sessionfee.transform import dropping_total, group_total


def _amounts(document_name: str) -> list[Quantity]:
    rows = rows_from_document(load_json(document_name))
    amounts: list[Quantity] = []
    for row in rows:
        if isinstance(row.amount, Quantity):
            amounts.append(row.amount)
    return amounts


def _chain(length: int) -> dict[str, str]:
    formulas: dict[str, str] = {}
    previous = "B2"
    for offset in range(length):
        column = chr(ord("C") + offset)
        cell = f"{column}2"
        formulas[cell] = f"=SUM({previous})"
        previous = cell
    return formulas


def run_controls() -> dict[str, object]:
    fixture_a = load_json("fixture_a_cell_valid_wrong_total.json")
    rows_a = rows_from_document(fixture_a)
    schema_a = check_rows(rows_a)
    invariant_a = check_invariant(
        written_total=decimal_field(fixture_a, "written_total_jpy"),
        expected_total=decimal_field(fixture_a, "expected_total_jpy"),
        written_row_count=len(rows_a),
        expected_row_count=int(fixture_a["expected_row_count"]),
    )
    fixture_b = load_json("fixture_b_non_numeric_amount.json")
    schema_b = check_rows(rows_from_document(fixture_b))
    fixture_c = load_json("fixture_c_shared_oracle.json")
    rows_c = rows_from_document(fixture_c)
    amounts_c = [row.amount for row in rows_c if isinstance(row.amount, Quantity)]
    dropped = dropping_total(amounts_c)
    shared_pass = check_invariant(
        written_total=dropped.value,
        expected_total=dropped.value,
        written_row_count=len(rows_c),
        expected_row_count=len(rows_c),
    ).passed
    literal_pass = check_invariant(
        written_total=dropped.value,
        expected_total=decimal_field(fixture_c, "expected_total_jpy"),
        written_row_count=len(rows_c),
        expected_row_count=int(fixture_c["expected_row_count"]),
    ).passed

    jpy_plus_jpy = "pass"
    try:
        add(Quantity(Decimal("1000"), JPY), Quantity(Decimal("500"), JPY))
    except DimensionMismatch:
        jpy_plus_jpy = "fail"
    jpy_plus_hours = "pass"
    try:
        add(Quantity(Decimal("1000"), JPY), Quantity(Decimal("2"), HOUR))
    except DimensionMismatch:
        jpy_plus_hours = "fail"
    product = multiply(Quantity(Decimal("2"), HOUR), Quantity(Decimal("4000"), JPY_PER_HOUR))
    comparison = "pass"
    try:
        compare(Quantity(Decimal("1000"), JPY), Quantity(Decimal("2"), HOUR), ">")
    except DimensionMismatch:
        comparison = "fail"
    dimensionless = "pass"
    try:
        add(Quantity(Decimal("3"), DIMENSIONLESS), Quantity(Decimal("2"), HOUR))
    except DimensionMismatch:
        dimensionless = "fail"
    header = "declared"
    try:
        dimension_for_column("Amount")
    except UndeclaredDimension:
        header = "undeclared"
    fahrenheit = "converted"
    try:
        fahrenheit_to_celsius(Decimal("68"))
    except UnsupportedConversion:
        fahrenheit = "unsupported"

    sum_range = evaluate_formula("=SUM(A2:A10)")
    conditional = evaluate_formula("=IF(A2,1,0)")
    two_cells = evaluate_formula("=SUM(A2,B2)")
    deep_sum = evaluate_formula("=SUM(SUM(SUM(A2)))")
    wide = evaluate_formula("=SUM(A2:A100)")
    chain8 = evaluate_chain(_chain(8), LOCAL_CHAIN_CEILING)
    chain4 = evaluate_chain(_chain(4), LOCAL_CHAIN_CEILING)

    session = SessionInput("op-1", "2026-10-07", "DESK-NORTH", Decimal("2.5"), Decimal("4000"))
    canonical = session.canonical()
    row = {"operation_id": "op-1"}
    append = AppendBaseline()
    append.exchange("op-1", canonical, row)
    append.exchange("op-1", canonical, row)
    ledger = Ledger()
    first = ledger.exchange("op-1", canonical, row)
    second = ledger.exchange("op-1", canonical, row)
    other = dict(canonical)
    other["hours"] = {"dimension": "hour", "value": "3"}
    conflict_ledger = Ledger()
    conflict_ledger.exchange("op-1", canonical, row)
    conflict = conflict_ledger.exchange("op-1", other, row)
    missing = Ledger()
    missing.exchange("", canonical, row)
    left = SessionInput("op-left", "2026-10-07", "DESK-NORTH", Decimal("1"), Decimal("3500"))
    right = SessionInput("op-right", "2026-10-07", "DESK-NORTH", Decimal("1"), Decimal("3500"))
    content = ContentHashBaseline()
    content.exchange(left.operation_id, left.canonical(), {"id": "left"})
    content.exchange(right.operation_id, right.canonical(), {"id": "right"})
    two_keys = Ledger()
    two_keys.exchange(left.operation_id, left.canonical(), {"id": "left"})
    two_keys.exchange(right.operation_id, right.canonical(), {"id": "right"})
    stubbed = user_entered_stub(canonical)

    mixed = BatchPlan(
        requests=(
            UpdateRequest(writes=(), valid=False, reason="missing range"),
            UpdateRequest(
                writes=(CellWrite(0, 1, 0, "DESK-NORTH", "RAW"),),
                valid=True,
            ),
        ),
        expected_row_version=7,
    )
    batch_sheet = MemorySheet(row_version=7)
    commit_batch(batch_sheet, mixed)
    separate_sheet = MemorySheet(row_version=7)
    apply_separate(separate_sheet, mixed)
    atomic = MemorySheet(row_version=7)
    commit_batch(
        atomic,
        BatchPlan(
            requests=(
                UpdateRequest(
                    writes=(
                        CellWrite(0, 1, 0, "DESK-NORTH", "RAW"),
                        CellWrite(0, 1, 1, Decimal("2.5"), "RAW"),
                    ),
                    valid=True,
                ),
            ),
            expected_row_version=7,
        ),
    )
    atomic.collaborator_edit(0, 1, 0, "DESK-EAST")
    second_plan = BatchPlan(
        requests=(
            UpdateRequest(writes=(CellWrite(0, 2, 0, "late", "RAW"),), valid=True),
        ),
        expected_row_version=7,
    )
    stale = commit_batch(atomic, second_plan)

    return {
        "fixture_a": {
            "schema_pass": schema_a.passed,
            "dimension_pass": all(dimensions_consistent(row) for row in rows_a),
            "invariant_pass": invariant_a.passed,
            "schema_failure_count": len(schema_a.errors),
        },
        "fixture_b": {"schema_pass": schema_b.passed},
        "fixture_c": {
            "shared_function_oracle_pass": shared_pass,
            "literal_oracle_pass": literal_pass,
        },
        "dimensions": {
            "jpy_plus_jpy": jpy_plus_jpy,
            "numeric_baseline_jpy_plus_hours": str(numeric_add(Decimal("1000"), Decimal("2"))),
            "jpy_plus_hours": jpy_plus_hours,
            "hours_times_rate": product.dimension.label(),
            "jpy_gt_hours": comparison,
            "dimensionless_plus_hours": dimensionless,
            "header_amount": header,
            "fahrenheit": fahrenheit,
            "jpy_times_jpy_equals_jpy": multiply(
                Quantity(Decimal("2"), JPY), Quantity(Decimal("3"), JPY)
            ).dimension
            == JPY,
        },
        "formulas": {
            "sum_range_accepted": sum_range.accepted,
            "sum_range_height": sum_range.height,
            "sum_range_ranges": sum_range.range_count,
            "if_accepted": conditional.accepted,
            "if_failures": list(conditional.failures),
            "sum_two_cells_accepted": two_cells.accepted,
            "deep_sum_accepted": deep_sum.accepted,
            "deep_sum_height": deep_sum.height,
            "wide_range_reference_count": wide.reference_count,
            "wide_range_accepted": wide.accepted,
            "chain8_per_formula": chain8.per_formula_accepted,
            "chain8_length": chain8.length,
            "chain8_ceiling_accepted": chain8.ceiling_accepted,
            "chain4_length": chain4.length,
            "chain4_ceiling_accepted": chain4.ceiling_accepted,
        },
        "idempotency": {
            "append_baseline_rows": len(append.rows),
            "ledger_same_key_rows": len(ledger.rows),
            "first_outcome": first.name,
            "second_outcome": second.name,
            "payload_conflict": conflict.name,
            "payload_conflict_rows": len(conflict_ledger.rows),
            "missing_key_rows": len(missing.rows),
            "content_hash_two_keys_rows": len(content.rows),
            "ledger_two_keys_rows": len(two_keys.rows),
            "pre_coercion_stable": fingerprint(canonical) == fingerprint(session.canonical()),
            "stub_changes_fingerprint": fingerprint(canonical) != fingerprint(stubbed),
        },
        "commit": {
            "batch_mixed_committed_cells": len(batch_sheet.committed),
            "separate_mixed_committed_cells": len(separate_sheet.committed),
            "collaborator_value": atomic.committed[(0, 1, 0)]["value"],
            "stale_outcome": stale.outcome,
            "stale_wrote_late_cell": (0, 2, 0) in atomic.committed,
            "final_version": atomic.row_version,
        },
        "serials": {
            "jan1_noon": str(to_serial(datetime(1900, 1, 1, 12, 0))),
            "feb1_1500": str(to_serial(datetime(1900, 2, 1, 15, 0))),
            "mar1_derived": str(to_serial(datetime(1900, 3, 1))),
            "leap_bug_mar1": str(leap_year_bug_serial(datetime(1900, 3, 1))),
            "feb28_to_mar1_step": str(to_serial(datetime(1900, 3, 1)) - to_serial(datetime(1900, 2, 28))),
        },
        "group_total_matches_hand_sum": group_total(_amounts("fixture_a_cell_valid_wrong_total.json")).value
        == Decimal("10000") + Decimal("3500") + Decimal("2000"),
    }
