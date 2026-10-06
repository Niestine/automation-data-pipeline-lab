"""spreadsheets.batchUpdate-shaped plans.

One logical commit is one request list. Every request is validated before
any pending write. An invalid request leaves the committed sheet unchanged.
Staged writes become visible together when the sheet is flushed. A caller
that still holds an older row version gets stale_version and writes nothing.

``row_version`` is a local stand-in for the documented collaborator hazard.
It is not an HTTP conditional request: the archived batchUpdate page does
not define an ETag or If-Match, and the archived RFC 9110 extract used for
this lab does not contain the conditional-request section.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sessionfee.coercion import store_value


@dataclass(frozen=True)
class CellWrite:
    sheet_id: int
    row: int
    column: int
    value: Any
    value_input_option: str


@dataclass(frozen=True)
class UpdateRequest:
    writes: tuple[CellWrite, ...]
    valid: bool
    reason: str = ""


@dataclass(frozen=True)
class BatchPlan:
    requests: tuple[UpdateRequest, ...]
    expected_row_version: int | None
    fail_at: int | None = None


@dataclass
class StageResult:
    outcome: str
    mutated: int
    errors: tuple[str, ...] = ()
    row_version: int | None = None


@dataclass
class MemorySheet:
    row_version: int = 0
    committed: dict[tuple[int, int, int], dict[str, Any]] = field(default_factory=dict)
    pending: dict[tuple[int, int, int], dict[str, Any]] = field(default_factory=dict)
    collaborator_edits: list[tuple[int, int, int]] = field(default_factory=list)

    def collaborator_edit(self, sheet_id: int, row: int, column: int, value: Any) -> None:
        key = (sheet_id, row, column)
        self.committed[key] = {"input": "collaborator", "kind": "text", "value": str(value)}
        self.collaborator_edits.append(key)

    def discard_pending(self) -> None:
        self.pending.clear()

    def flush(self) -> int:
        if not self.pending:
            return 0
        count = len(self.pending)
        self.committed.update(self.pending)
        self.pending.clear()
        self.row_version += 1
        return count


def _validate(plan: BatchPlan) -> list[str]:
    errors: list[str] = []
    for index, request in enumerate(plan.requests):
        if not request.valid:
            errors.append(request.reason or f"request {index} invalid")
            continue
        if not request.writes:
            errors.append(f"request {index} has no writes")
        for write in request.writes:
            if write.value_input_option not in ("RAW", "USER_ENTERED"):
                errors.append(f"request {index} valueInputOption")
            if write.row < 0 or write.column < 0 or write.sheet_id < 0:
                errors.append(f"request {index} range")
    return errors


def stage_batch(sheet: MemorySheet, plan: BatchPlan) -> StageResult:
    """Validate the whole list, then stage every write or stage none."""

    errors = _validate(plan)
    if errors:
        return StageResult("apply_none", 0, tuple(errors))
    if plan.expected_row_version is not None and plan.expected_row_version != sheet.row_version:
        return StageResult("stale_version", 0, row_version=sheet.row_version)
    staged: dict[tuple[int, int, int], dict[str, Any]] = {}
    position = 0
    for request in plan.requests:
        for write in request.writes:
            if plan.fail_at is not None and position == plan.fail_at:
                return StageResult("rolled_back", 0, ("injected fault",))
            key = (write.sheet_id, write.row, write.column)
            staged[key] = store_value(write.value, write.value_input_option)
            position += 1
    sheet.pending.update(staged)
    return StageResult("staged", position, row_version=sheet.row_version)


def commit_batch(sheet: MemorySheet, plan: BatchPlan) -> StageResult:
    """Stage and flush. Readers see every write from the list, or none."""

    staged = stage_batch(sheet, plan)
    if staged.outcome != "staged":
        sheet.discard_pending()
        return staged
    sheet.flush()
    return StageResult("applied", staged.mutated, row_version=sheet.row_version)


def apply_separate(sheet: MemorySheet, plan: BatchPlan) -> StageResult:
    """Baseline: each valid request hits the committed cells on its own.

    An invalid request is skipped. Earlier valid writes stay. There is no
    version check and no flush boundary.
    """

    mutated = 0
    for request in plan.requests:
        if not request.valid:
            continue
        for write in request.writes:
            if write.value_input_option not in ("RAW", "USER_ENTERED"):
                continue
            if write.row < 0 or write.column < 0:
                continue
            key = (write.sheet_id, write.row, write.column)
            sheet.committed[key] = store_value(write.value, write.value_input_option)
            mutated += 1
    return StageResult("partial", mutated, row_version=sheet.row_version)
