"""Local operation-id ledger.

The state machine follows the idempotency-key draft's outcomes: first sight
processes, a completed duplicate replays the stored result, a retry while
the first attempt is in progress conflicts, and the same key with a different
fingerprint conflicts. The draft is expired work in progress relative to
2026-10-07, so these are local outcome names. HTTP status numbers are comments
for the Apps Script example, not a claim that the sheet speaks HTTP.

A missing key writes nothing. Two different keys with the same business
fields are two operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sessionfee.canonical import fingerprint


@dataclass(frozen=True)
class LedgerOutcome:
    name: str
    http_comment: int | None
    fingerprint: str | None = None
    result: dict[str, Any] | None = None


class Ledger:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []

    def open_attempt(self, operation_id: str | None, canonical: dict[str, Any]) -> LedgerOutcome:
        if operation_id is None or str(operation_id).strip() == "":
            return LedgerOutcome("missing_key", 400)
        key = str(operation_id)
        digest = fingerprint(canonical)
        existing = self.entries.get(key)
        if existing is None:
            self.entries[key] = {"state": "in_progress", "fingerprint": digest, "result": None}
            return LedgerOutcome("started", None, digest)
        if existing["fingerprint"] != digest:
            return LedgerOutcome("payload_conflict", 422, digest)
        if existing["state"] == "in_progress":
            return LedgerOutcome("in_progress_conflict", 409, digest)
        return LedgerOutcome("replay", 200, digest, existing["result"])

    def abort_attempt(self, operation_id: str) -> None:
        entry = self.entries.get(operation_id)
        if entry and entry["state"] == "in_progress":
            del self.entries[operation_id]

    def finish_attempt(self, operation_id: str, result: dict[str, Any], row: dict[str, Any]) -> None:
        entry = self.entries[operation_id]
        if entry["state"] != "in_progress":
            raise RuntimeError("finish_attempt requires an in-progress operation")
        entry["state"] = "completed"
        entry["result"] = result
        self.rows.append(row)

    def exchange(
        self,
        operation_id: str | None,
        canonical: dict[str, Any],
        row: dict[str, Any],
    ) -> LedgerOutcome:
        opened = self.open_attempt(operation_id, canonical)
        if opened.name != "started":
            return opened
        assert opened.fingerprint is not None
        result = {
            "operation_id": operation_id,
            "fingerprint": opened.fingerprint,
            "stored": True,
        }
        self.finish_attempt(str(operation_id), result, row)
        return LedgerOutcome("applied", 200, opened.fingerprint, result)


class AppendBaseline:
    """Control: every call appends, including a retry of the same payload."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def exchange(
        self,
        operation_id: str | None,
        canonical: dict[str, Any],
        row: dict[str, Any],
    ) -> LedgerOutcome:
        self.rows.append(row)
        return LedgerOutcome("applied", None, fingerprint(canonical))


class ContentHashBaseline:
    """Control: deduplicate on business fields and ignore the operation id.

    A second legitimate event with the same hours, rate, date, and desk is
    dropped. The operation-id ledger must not do that.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def exchange(
        self,
        operation_id: str | None,
        canonical: dict[str, Any],
        row: dict[str, Any],
    ) -> LedgerOutcome:
        business = {key: value for key, value in canonical.items() if key != "operation_id"}
        digest = fingerprint(business)
        if digest in self._seen:
            return LedgerOutcome("dropped", None, digest)
        self._seen.add(digest)
        self.rows.append(row)
        return LedgerOutcome("applied", None, digest)
