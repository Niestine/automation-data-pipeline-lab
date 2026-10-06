"""Validate-then-apply plans, row versions, and the script-lock stub."""

from __future__ import annotations

import unittest
from decimal import Decimal

import support  # noqa: F401

from sessionfee.canonical import SessionInput
from sessionfee.commit import build_data_plan, commit_week, commit_with_lock
from sessionfee.fixtures import decimal_field, load_json, sessions_from_document
from sessionfee.formula_budget import evaluate_formula
from sessionfee.idempotency import Ledger
from sessionfee.lockstub import LockStub, LockTimeout
from sessionfee.observe import RunLog
from sessionfee.plan import BatchPlan, CellWrite, MemorySheet, UpdateRequest, apply_separate, commit_batch, stage_batch


def _mixed_plan() -> BatchPlan:
    return BatchPlan(
        requests=(
            UpdateRequest(writes=(), valid=False, reason="missing range"),
            UpdateRequest(writes=(CellWrite(0, 1, 0, "DESK-NORTH", "RAW"),), valid=True),
        ),
        expected_row_version=7,
    )


class CommitTests(unittest.TestCase):
    def test_invalid_plus_valid_plan_mutates_nothing(self) -> None:
        sheet = MemorySheet(row_version=7)
        result = commit_batch(sheet, _mixed_plan())
        self.assertEqual(result.outcome, "apply_none")
        self.assertEqual(result.mutated, 0)
        self.assertEqual(sheet.committed, {})
        self.assertEqual(sheet.pending, {})
        self.assertEqual(sheet.row_version, 7)

    def test_separate_writes_baseline_keeps_valid_write(self) -> None:
        sheet = MemorySheet(row_version=7)
        result = apply_separate(sheet, _mixed_plan())
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.mutated, 1)
        self.assertEqual(sheet.committed[(0, 1, 0)]["value"], "DESK-NORTH")
        self.assertEqual(len(sheet.committed), 1)

    def test_staged_batch_is_invisible_until_flush(self) -> None:
        sheet = MemorySheet(row_version=7)
        plan = BatchPlan(
            requests=(
                UpdateRequest(
                    writes=(
                        CellWrite(0, 1, 0, "DESK-NORTH", "RAW"),
                        CellWrite(0, 1, 3, Decimal("2.5"), "RAW"),
                    ),
                    valid=True,
                ),
            ),
            expected_row_version=7,
        )
        staged = stage_batch(sheet, plan)
        self.assertEqual(staged.outcome, "staged")
        self.assertEqual(sheet.committed, {})
        self.assertEqual(len(sheet.pending), 2)
        self.assertEqual(sheet.row_version, 7)
        self.assertEqual(sheet.flush(), 2)
        self.assertEqual(len(sheet.committed), 2)
        self.assertEqual(sheet.row_version, 8)
        self.assertEqual(sheet.committed[(0, 1, 3)]["kind"], "number")

    def test_injected_fault_rolls_back_before_any_pending_write(self) -> None:
        sheet = MemorySheet(row_version=3)
        plan = BatchPlan(
            requests=(
                UpdateRequest(
                    writes=(
                        CellWrite(0, 1, 0, "DESK-NORTH", "RAW"),
                        CellWrite(0, 1, 1, "2026-10-05", "RAW"),
                    ),
                    valid=True,
                ),
            ),
            expected_row_version=3,
            fail_at=1,
        )
        result = commit_batch(sheet, plan)
        self.assertEqual(result.outcome, "rolled_back")
        self.assertEqual(sheet.committed, {})
        self.assertEqual(sheet.pending, {})
        self.assertEqual(sheet.row_version, 3)

    def test_collaborator_edit_survives_atomic_apply(self) -> None:
        sheet = MemorySheet(row_version=7)
        commit_batch(
            sheet,
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
        sheet.collaborator_edit(0, 1, 0, "DESK-EAST")
        self.assertEqual(sheet.committed[(0, 1, 0)]["value"], "DESK-EAST")
        self.assertEqual(sheet.committed[(0, 1, 1)]["value"], "2.5")
        self.assertEqual(sheet.collaborator_edits, [(0, 1, 0)])

    def test_stale_version_second_plan_writes_nothing(self) -> None:
        sheet = MemorySheet(row_version=7)
        first = BatchPlan(
            requests=(UpdateRequest(writes=(CellWrite(0, 1, 0, "first", "RAW"),), valid=True),),
            expected_row_version=7,
        )
        second = BatchPlan(
            requests=(UpdateRequest(writes=(CellWrite(0, 2, 0, "late", "RAW"),), valid=True),),
            expected_row_version=7,
        )
        applied = commit_batch(sheet, first)
        stale = commit_batch(sheet, second)
        self.assertEqual(applied.outcome, "applied")
        self.assertEqual(sheet.row_version, 8)
        self.assertEqual(stale.outcome, "stale_version")
        self.assertNotIn((0, 2, 0), sheet.committed)
        self.assertEqual(sheet.committed[(0, 1, 0)]["value"], "first")

    def test_held_lock_does_not_apply(self) -> None:
        sheet = MemorySheet(row_version=7)
        ledger_cells = dict(sheet.committed)
        lock = LockStub(held_by_other=True)
        plan = BatchPlan(
            requests=(UpdateRequest(writes=(CellWrite(0, 1, 0, "x", "RAW"),), valid=True),),
            expected_row_version=7,
        )
        report = commit_with_lock(lock, sheet, plan)
        self.assertEqual(report.outcome, "not_acquired")
        self.assertEqual(sheet.committed, ledger_cells)
        self.assertEqual(sheet.row_version, 7)
        self.assertEqual([name for name, _timeout in lock.events], ["try_lock"])
        self.assertFalse(lock.has_lock())

    def test_lock_flush_is_recorded_before_release(self) -> None:
        sheet = MemorySheet(row_version=7)
        lock = LockStub()
        plan = BatchPlan(
            requests=(UpdateRequest(writes=(CellWrite(0, 1, 0, "x", "RAW"),), valid=True),),
            expected_row_version=7,
        )
        report = commit_with_lock(lock, sheet, plan)
        self.assertEqual(report.outcome, "applied")
        names = [name for name, _timeout in lock.events]
        self.assertEqual(names, ["try_lock", "flush", "release"])
        self.assertFalse(lock.acquired)
        self.assertEqual(sheet.row_version, 8)

    def test_lock_flush_commits_pending_cells_before_release(self) -> None:
        sheet = MemorySheet(row_version=2)
        lock = LockStub()
        self.assertTrue(lock.try_lock(10))
        plan = BatchPlan(
            requests=(UpdateRequest(writes=(CellWrite(0, 1, 0, "pending", "RAW"),), valid=True),),
            expected_row_version=2,
        )
        self.assertEqual(stage_batch(sheet, plan).outcome, "staged")
        self.assertEqual(sheet.committed, {})
        self.assertEqual(lock.flush(sheet), 1)
        self.assertTrue(lock.has_lock())
        self.assertEqual(sheet.committed[(0, 1, 0)]["value"], "pending")
        lock.release()
        self.assertEqual(lock.events, [("try_lock", 10), ("flush", 1), ("release", None)])

    def test_wait_lock_raises_when_held(self) -> None:
        lock = LockStub(held_by_other=True)
        with self.assertRaises(LockTimeout):
            lock.wait_lock(10)

    def test_formula_budget_failure_makes_the_plan_apply_none(self) -> None:
        rejected = evaluate_formula("=IF(A2>0,B2*C2,0)")
        self.assertFalse(rejected.accepted)
        sheet = MemorySheet(row_version=4)
        plan = build_data_plan(
            [
                {
                    "operation_id": "op-1",
                    "session_date": "2026-10-05",
                    "desk_code": "DESK-NORTH",
                    "hours": Decimal("2.5"),
                    "rate_jpy_per_hour": Decimal("4000"),
                    "amount_jpy": Decimal("10000"),
                    "fingerprint": "abc",
                }
            ],
            "=IF(A2>0,B2*C2,0)",
            False,
            4,
        )
        result = commit_batch(sheet, plan)
        self.assertEqual(result.outcome, "apply_none")
        self.assertEqual(sheet.committed, {})
        self.assertEqual(sheet.row_version, 4)

    def test_discard_without_flush_leaves_committed_cells_unchanged(self) -> None:
        sheet = MemorySheet(row_version=2)
        plan = BatchPlan(
            requests=(UpdateRequest(writes=(CellWrite(0, 1, 0, "pending", "RAW"),), valid=True),),
            expected_row_version=2,
        )
        self.assertEqual(stage_batch(sheet, plan).outcome, "staged")
        sheet.discard_pending()
        self.assertEqual(sheet.committed, {})
        self.assertEqual(sheet.row_version, 2)


class WeekCommitTests(unittest.TestCase):
    def _document(self) -> dict:
        return load_json("week_sessions.json")

    def test_dry_run_does_not_mutate_and_logs_the_outcome(self) -> None:
        document = self._document()
        sheet = MemorySheet(row_version=0)
        ledger = Ledger()
        log = RunLog()
        report = commit_week(
            sheet,
            ledger,
            LockStub(),
            sessions_from_document(document),
            expected_total=decimal_field(document, "expected_total_jpy"),
            expected_row_count=int(document["expected_row_count"]),
            read_version=0,
            dry_run=True,
            log=log,
        )
        self.assertEqual(report.outcome, "dry_run")
        self.assertTrue(report.dry_run)
        self.assertEqual(sheet.committed, {})
        self.assertEqual(ledger.rows, [])
        self.assertEqual(sheet.row_version, 0)
        self.assertEqual(report.formula, "=SUM(F2:F4)")
        self.assertEqual(report.written_total, "15500")
        self.assertTrue(evaluate_formula(report.formula or "").accepted)
        self.assertEqual(log.events[0]["outcome"], "dry_run")
        self.assertTrue(log.events[0]["dry_run"])

    def test_commit_writes_raw_identity_and_replays_the_same_week(self) -> None:
        document = self._document()
        sheet = MemorySheet(row_version=7)
        ledger = Ledger()
        sessions = sessions_from_document(document)
        applied = commit_week(
            sheet,
            ledger,
            LockStub(),
            sessions,
            expected_total=decimal_field(document, "expected_total_jpy"),
            expected_row_count=int(document["expected_row_count"]),
            read_version=7,
        )
        self.assertEqual(applied.outcome, "applied")
        self.assertEqual(sheet.row_version, 8)
        self.assertEqual(len(ledger.rows), 3)
        date_cell = sheet.committed[(0, 1, 1)]
        self.assertEqual(date_cell["input"], "RAW")
        self.assertEqual(date_cell["kind"], "text")
        self.assertEqual(date_cell["value"], "2026-10-05")
        operation_cell = sheet.committed[(0, 1, 0)]
        self.assertEqual(operation_cell["kind"], "text")
        formula_cell = sheet.committed[(0, 4, 5)]
        self.assertEqual(formula_cell["input"], "USER_ENTERED")
        self.assertEqual(formula_cell["kind"], "formula")
        self.assertEqual(formula_cell["value"], "=SUM(F2:F4)")
        replay = commit_week(
            sheet,
            ledger,
            LockStub(),
            sessions,
            expected_total=decimal_field(document, "expected_total_jpy"),
            expected_row_count=int(document["expected_row_count"]),
            read_version=8,
        )
        self.assertEqual(replay.outcome, "replay")
        self.assertEqual(len(ledger.rows), 3)
        self.assertEqual(sheet.row_version, 8)

    def test_commit_refuses_a_disagreeing_literal_total(self) -> None:
        document = self._document()
        sheet = MemorySheet(row_version=0)
        ledger = Ledger()
        report = commit_week(
            sheet,
            ledger,
            LockStub(),
            sessions_from_document(document),
            expected_total=Decimal("10000"),
            expected_row_count=int(document["expected_row_count"]),
            read_version=0,
        )
        self.assertEqual(report.outcome, "invariant_reject")
        self.assertEqual(sheet.committed, {})
        self.assertEqual(ledger.rows, [])

    def test_held_lock_leaves_the_week_ledger_unchanged(self) -> None:
        document = self._document()
        sheet = MemorySheet(row_version=0)
        ledger = Ledger()
        report = commit_week(
            sheet,
            ledger,
            LockStub(held_by_other=True),
            sessions_from_document(document),
            expected_total=decimal_field(document, "expected_total_jpy"),
            expected_row_count=int(document["expected_row_count"]),
            read_version=0,
        )
        self.assertEqual(report.outcome, "not_acquired")
        self.assertEqual(ledger.entries, {})
        self.assertEqual(sheet.committed, {})

    def test_stale_reader_does_not_append_a_different_operation(self) -> None:
        document = self._document()
        sheet = MemorySheet(row_version=7)
        ledger = Ledger()
        sessions = sessions_from_document(document)
        first = commit_week(
            sheet,
            ledger,
            LockStub(),
            sessions,
            expected_total=decimal_field(document, "expected_total_jpy"),
            expected_row_count=3,
            read_version=7,
        )
        self.assertEqual(first.outcome, "applied")
        late = sessions_from_document(document)[0]
        other = SessionInput(
            late.operation_id + "-other",
            late.session_date,
            late.desk_code,
            late.hours,
            late.rate_jpy_per_hour,
        )
        stale = commit_week(
            sheet,
            ledger,
            LockStub(),
            [other, sessions[1], sessions[2]],
            expected_total=decimal_field(document, "expected_total_jpy"),
            expected_row_count=3,
            read_version=7,
        )
        self.assertEqual(stale.outcome, "stale_version")
        self.assertNotIn(other.operation_id, ledger.entries)
        self.assertEqual(len(ledger.rows), 3)

    def test_late_session_moves_the_formula_and_keeps_stored_amounts(self) -> None:
        document = self._document()
        sheet = MemorySheet(row_version=0)
        ledger = Ledger()
        first = commit_week(
            sheet,
            ledger,
            LockStub(),
            sessions_from_document(document),
            expected_total=decimal_field(document, "expected_total_jpy"),
            expected_row_count=3,
            read_version=0,
        )
        self.assertEqual(first.outcome, "applied")
        self.assertEqual(sheet.committed[(0, 4, 5)]["value"], "=SUM(F2:F4)")
        late = SessionInput("op-late", "2026-10-08", "DESK-SOUTH", Decimal("1"), Decimal("3000"))
        second = commit_week(
            sheet,
            ledger,
            LockStub(),
            [late],
            expected_total=Decimal("3000"),
            expected_row_count=1,
            read_version=1,
        )
        self.assertEqual(second.outcome, "applied")
        amounts = [sheet.committed[(0, row, 5)]["value"] for row in range(1, 5)]
        self.assertEqual(amounts, ["10000", "3500", "2000", "3000"])
        self.assertEqual(sheet.committed[(0, 4, 0)]["value"], "op-late")
        self.assertEqual(second.formula, "=SUM(F2:F5)")
        self.assertEqual(sheet.committed[(0, 5, 5)]["value"], "=SUM(F2:F5)")
        self.assertEqual(sheet.committed[(0, 5, 5)]["kind"], "formula")
        self.assertEqual(len(ledger.rows), 4)

    def test_empty_week_writes_nothing(self) -> None:
        report = commit_week(
            MemorySheet(),
            Ledger(),
            LockStub(),
            [],
            expected_total=Decimal("0"),
            expected_row_count=0,
            read_version=0,
        )
        self.assertEqual(report.outcome, "empty")

    def test_zero_hours_commit_when_the_literal_total_is_zero(self) -> None:
        session = SessionInput("op-zero", "2026-10-05", "DESK-NORTH", Decimal("0"), Decimal("4000"))
        sheet = MemorySheet(row_version=0)
        report = commit_week(
            sheet,
            Ledger(),
            LockStub(),
            [session],
            expected_total=Decimal("0"),
            expected_row_count=1,
            read_version=0,
        )
        self.assertEqual(report.outcome, "applied")
        self.assertEqual(sheet.committed[(0, 1, 3)]["value"], "0")
        self.assertEqual(sheet.committed[(0, 1, 5)]["value"], "0")

    def test_negative_hours_are_a_schema_reject(self) -> None:
        session = SessionInput("op-neg", "2026-10-05", "DESK-NORTH", Decimal("-1"), Decimal("4000"))
        report = commit_week(
            MemorySheet(),
            Ledger(),
            LockStub(),
            [session],
            expected_total=Decimal("-4000"),
            expected_row_count=1,
            read_version=0,
        )
        self.assertEqual(report.outcome, "schema_reject")


if __name__ == "__main__":
    unittest.main()
