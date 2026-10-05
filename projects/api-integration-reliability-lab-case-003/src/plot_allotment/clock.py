"""Injectable clock. The demo and tests never sleep on the wall clock."""

from __future__ import annotations


class ManualClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)
