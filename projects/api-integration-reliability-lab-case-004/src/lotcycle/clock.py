"""Virtual clock. Tests advance time; nothing sleeps on the wall clock."""

from __future__ import annotations


class VirtualClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("virtual time only moves forward")
        self._now += float(seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
