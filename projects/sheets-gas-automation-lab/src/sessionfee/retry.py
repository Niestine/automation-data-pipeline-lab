"""Deterministic retry for an in-progress operation conflict.

Delays are recorded by an injected sleeper. The lab does not sleep on a
wall clock and does not contact Google.
"""

from __future__ import annotations

from typing import Any, Callable

from sessionfee.idempotency import Ledger, LedgerOutcome


def retry_exchange(
    ledger: Ledger,
    operation_id: str | None,
    canonical: dict[str, Any],
    row: dict[str, Any],
    schedule_ms: tuple[int, ...],
    sleep: Callable[[int], None],
) -> tuple[LedgerOutcome, list[int]]:
    """Resend the same id and payload after an in-progress conflict.

    The first attempt uses no delay. Later attempts sleep for the scheduled
    milliseconds, then send the unchanged request. A payload conflict is not
    retried because the draft tells the client to correct that request.
    """

    delays: list[int] = []
    last = LedgerOutcome("missing_key", 400)
    for index, delay in enumerate(schedule_ms):
        if index > 0:
            sleep(delay)
            delays.append(delay)
        last = ledger.exchange(operation_id, canonical, row)
        if last.name != "in_progress_conflict":
            return last, delays
    return last, delays
