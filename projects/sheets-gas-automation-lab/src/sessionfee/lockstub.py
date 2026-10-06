"""In-process stand-in for Apps Script Lock.

try_lock returns false when another holder already has the lock.
wait_lock raises instead. flush(sheet) flushes the sheet's pending cells
and records the event, so the event order shows the spreadsheet note was
followed: flush pending changes, then release.
The stub is not LockService and the tests do not execute Apps Script.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class _Flushable(Protocol):
    def flush(self) -> int: ...


class LockTimeout(Exception):
    """Raised by wait_lock when the lock is not acquired."""


@dataclass
class LockStub:
    held_by_other: bool = False
    acquired: bool = False
    events: list[tuple[str, int | None]] = field(default_factory=list)

    def try_lock(self, timeout_ms: int) -> bool:
        self.events.append(("try_lock", timeout_ms))
        if self.acquired:
            return True
        if self.held_by_other:
            return False
        self.acquired = True
        return True

    def wait_lock(self, timeout_ms: int) -> None:
        if not self.try_lock(timeout_ms):
            raise LockTimeout(f"lock not acquired within {timeout_ms} ms")

    def flush(self, sheet: _Flushable | None = None) -> int:
        """Stand-in for SpreadsheetApp.flush() while the lock is held."""

        flushed = sheet.flush() if sheet is not None else 0
        self.events.append(("flush", flushed))
        return flushed

    def release(self) -> None:
        self.events.append(("release", None))
        self.acquired = False

    def has_lock(self) -> bool:
        return self.acquired
