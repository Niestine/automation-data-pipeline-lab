"""Commit one billing week as a single batch under a script lock.

The business total is computed in Python. The sheet receives RAW typed
cells plus one shallow SUM staging formula on the USER_ENTERED channel.
That formula is not part of the operation fingerprint.

The independent expected total is a caller-supplied literal. When it
disagrees with the Python total, nothing is written.

The ledger is the record of which session rows the sheet already holds.
Data row ``n`` of the ledger sits on 0-based sheet row ``n`` (row 0 is the
header), and the staging formula sits on the first row below the last
stored session, summing every stored amount. A call that adds one late
session therefore moves the formula down instead of overwriting a stored
amount.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sessionfee.canonical import SessionInput, decimal_string
from sessionfee.dimensions import Quantity
from sessionfee.formula_budget import evaluate_formula, staging_sum_formula
from sessionfee.idempotency import Ledger, LedgerOutcome
from sessionfee.invariants import check_invariant
from sessionfee.lockstub import LockStub
from sessionfee.observe import RunLog
from sessionfee.plan import (
    BatchPlan,
    CellWrite,
    MemorySheet,
    UpdateRequest,
    commit_batch,
)
from sessionfee.schema import SessionRow, check_rows
from sessionfee.transform import build_row, group_total

AMOUNT_COLUMN = 5
FORMULA_COLUMN = "F"


@dataclass
class CommitReport:
    outcome: str
    mutated: int = 0
    row_version: int | None = None
    errors: tuple[str, ...] = ()
    formula: str | None = None
    written_total: str | None = None
    operation_outcomes: tuple[str, ...] = ()
    dry_run: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)


def _row_payload(row: SessionRow, digest: str) -> dict[str, Any]:
    amount = row.amount
    if not isinstance(amount, Quantity):
        raise ValueError("cannot store a non-numeric amount")
    return {
        "amount_jpy": amount.value,
        "desk_code": row.desk_code,
        "fingerprint": digest,
        "hours": row.hours.value,
        "operation_id": row.operation_id,
        "rate_jpy_per_hour": row.rate.value,
        "session_date": row.session_date,
    }


def _staging_formula(stored_rows: int) -> str:
    """SUM over every stored amount. A1 rows 2..1+stored_rows hold sessions."""

    return staging_sum_formula(FORMULA_COLUMN, 2, 1 + max(stored_rows, 1))


def build_data_plan(
    payloads: list[dict[str, Any]],
    formula: str,
    formula_accepted: bool,
    expected_row_version: int,
    first_row: int = 1,
    formula_row: int | None = None,
) -> BatchPlan:
    """Build one batchUpdate-shaped list.

    Data cells use RAW. The staging formula uses USER_ENTERED and is a
    separate request so a budget failure marks the whole plan invalid.
    """

    writes: list[CellWrite] = []
    columns = (
        "operation_id",
        "session_date",
        "desk_code",
        "hours",
        "rate_jpy_per_hour",
        "amount_jpy",
        "fingerprint",
    )
    for offset, payload in enumerate(payloads):
        for column, name in enumerate(columns):
            writes.append(
                CellWrite(
                    sheet_id=0,
                    row=first_row + offset,
                    column=column,
                    value=payload[name],
                    value_input_option="RAW",
                )
            )
    data_request = UpdateRequest(writes=tuple(writes), valid=True)
    formula_at = first_row + len(payloads) if formula_row is None else formula_row
    formula_request = UpdateRequest(
        writes=(
            CellWrite(
                sheet_id=0,
                row=formula_at,
                column=AMOUNT_COLUMN,
                value=formula,
                value_input_option="USER_ENTERED",
            ),
        ),
        valid=formula_accepted,
        reason="" if formula_accepted else "formula budget",
    )
    return BatchPlan(
        requests=(data_request, formula_request),
        expected_row_version=expected_row_version,
    )


def commit_with_lock(lock: LockStub, sheet: MemorySheet, plan: BatchPlan, log: RunLog | None = None) -> CommitReport:
    """Acquire the script lock, flush pending changes, then release.

    A failed try_lock writes nothing and does not flush. The lock events
    record flush before release whenever the lock was held.
    """

    sink = log if log is not None else RunLog()
    if not lock.try_lock(10000):
        sink.add(event="commit", outcome="not_acquired", dry_run=False, row_version=sheet.row_version)
        return CommitReport(outcome="not_acquired", row_version=sheet.row_version, events=sink.events)
    try:
        result = commit_batch(sheet, plan)
        sink.add(
            event="commit",
            outcome=result.outcome,
            dry_run=False,
            mutated=result.mutated,
            row_version=sheet.row_version,
        )
        return CommitReport(
            outcome=result.outcome,
            mutated=result.mutated,
            row_version=sheet.row_version,
            errors=result.errors,
            events=sink.events,
        )
    finally:
        lock.flush(sheet)
        lock.release()


def commit_week(
    sheet: MemorySheet,
    ledger: Ledger,
    lock: LockStub,
    sessions: list[SessionInput],
    *,
    expected_total: Decimal,
    expected_row_count: int,
    read_version: int,
    dry_run: bool = False,
    log: RunLog | None = None,
) -> CommitReport:
    """Validate, then commit new sessions. Replays do not append a second row."""

    sink = log if log is not None else RunLog()
    if not sessions:
        sink.add(event="commit_week", outcome="empty", dry_run=dry_run, row_version=sheet.row_version)
        return CommitReport(outcome="empty", dry_run=dry_run, row_version=sheet.row_version, events=sink.events)

    built: list[tuple[SessionInput, SessionRow]] = []
    for session in sessions:
        built.append((session, build_row(session)))
    schema = check_rows([row for _session, row in built])
    if not schema.passed:
        sink.add(event="commit_week", outcome="schema_reject", dry_run=dry_run, errors=list(schema.errors))
        return CommitReport(
            outcome="schema_reject",
            errors=schema.errors,
            dry_run=dry_run,
            row_version=sheet.row_version,
            events=sink.events,
        )

    amounts = []
    for _session, row in built:
        if not isinstance(row.amount, Quantity):
            raise RuntimeError("schema passed a non-numeric amount")
        amounts.append(row.amount)
    written = group_total(amounts)
    invariant = check_invariant(
        written_total=written.value,
        expected_total=expected_total,
        written_row_count=len(built),
        expected_row_count=expected_row_count,
    )
    if not invariant.passed:
        sink.add(
            event="commit_week",
            outcome="invariant_reject",
            dry_run=dry_run,
            written_total=decimal_string(written.value),
            expected_total=decimal_string(expected_total),
        )
        return CommitReport(
            outcome="invariant_reject",
            written_total=decimal_string(written.value),
            dry_run=dry_run,
            row_version=sheet.row_version,
            errors=invariant.reasons,
            events=sink.events,
        )

    # Projection for the budget check and the dry run: rows already in the
    # ledger plus sessions it has not seen. The formula is rebuilt from the
    # opened attempts once the lock is held.
    unseen = sum(1 for session, _row in built if session.operation_id not in ledger.entries)
    formula = _staging_formula(len(ledger.rows) + unseen)
    budget = evaluate_formula(formula)
    if not budget.accepted:
        sink.add(
            event="commit_week",
            outcome="formula_reject",
            dry_run=dry_run,
            formula=formula,
            failures=list(budget.failures),
        )
        return CommitReport(
            outcome="formula_reject",
            formula=formula,
            errors=budget.failures,
            dry_run=dry_run,
            row_version=sheet.row_version,
            events=sink.events,
        )
    if not lock.try_lock(10000):
        sink.add(event="commit_week", outcome="not_acquired", dry_run=dry_run, row_version=sheet.row_version)
        return CommitReport(outcome="not_acquired", dry_run=dry_run, row_version=sheet.row_version, events=sink.events)
    try:
        if dry_run:
            sink.add(
                event="commit_week",
                outcome="dry_run",
                dry_run=True,
                row_version=sheet.row_version,
                formula=formula,
                sessions=len(sessions),
            )
            return CommitReport(
                outcome="dry_run",
                dry_run=True,
                formula=formula,
                written_total=decimal_string(written.value),
                row_version=sheet.row_version,
                events=sink.events,
            )
        if read_version != sheet.row_version:
            sink.add(event="commit_week", outcome="stale_version", dry_run=False, row_version=sheet.row_version)
            return CommitReport(outcome="stale_version", row_version=sheet.row_version, events=sink.events)

        opened: list[tuple[SessionInput, SessionRow, str]] = []
        outcomes: list[str] = []
        for session, row in built:
            canonical = session.canonical()
            decision: LedgerOutcome = ledger.open_attempt(session.operation_id, canonical)
            outcomes.append(decision.name)
            if decision.name == "replay":
                continue
            if decision.name != "started" or decision.fingerprint is None:
                for started, _started_row, _digest in opened:
                    ledger.abort_attempt(started.operation_id)
                sink.add(event="commit_week", outcome=decision.name, dry_run=False, row_version=sheet.row_version)
                return CommitReport(
                    outcome=decision.name,
                    operation_outcomes=tuple(outcomes),
                    row_version=sheet.row_version,
                    events=sink.events,
                )
            opened.append((session, row, decision.fingerprint))

        if not opened:
            sink.add(event="commit_week", outcome="replay", dry_run=False, row_version=sheet.row_version)
            return CommitReport(
                outcome="replay",
                operation_outcomes=tuple(outcomes),
                formula=formula,
                written_total=decimal_string(written.value),
                row_version=sheet.row_version,
                events=sink.events,
            )

        stored_after = len(ledger.rows) + len(opened)
        formula = _staging_formula(stored_after)
        if not evaluate_formula(formula).accepted:
            for session, _row, _digest in opened:
                ledger.abort_attempt(session.operation_id)
            sink.add(event="commit_week", outcome="formula_reject", dry_run=False, formula=formula)
            return CommitReport(
                outcome="formula_reject",
                formula=formula,
                row_version=sheet.row_version,
                events=sink.events,
            )
        payloads = [_row_payload(row, digest) for _session, row, digest in opened]
        plan = build_data_plan(
            payloads,
            formula,
            True,
            read_version,
            first_row=1 + len(ledger.rows),
            formula_row=1 + stored_after,
        )
        staged = commit_batch(sheet, plan)
        if staged.outcome != "applied":
            for session, _row, _digest in opened:
                ledger.abort_attempt(session.operation_id)
            sink.add(event="commit_week", outcome=staged.outcome, dry_run=False, errors=list(staged.errors))
            return CommitReport(
                outcome=staged.outcome,
                errors=staged.errors,
                formula=formula,
                row_version=sheet.row_version,
                events=sink.events,
            )
        for session, row, digest in opened:
            payload = _row_payload(row, digest)
            stored = {
                "operation_id": session.operation_id,
                "fingerprint": digest,
                "amount_jpy": str(payload["amount_jpy"]),
            }
            ledger.finish_attempt(session.operation_id, stored, stored)
        sink.add(
            event="commit_week",
            outcome="applied",
            dry_run=False,
            mutated=staged.mutated,
            row_version=sheet.row_version,
            formula=formula,
            sessions=len(opened),
        )
        return CommitReport(
            outcome="applied",
            mutated=staged.mutated,
            row_version=sheet.row_version,
            formula=formula,
            written_total=decimal_string(written.value),
            operation_outcomes=tuple(outcomes),
            events=sink.events,
        )
    finally:
        lock.flush(sheet)
        lock.release()
