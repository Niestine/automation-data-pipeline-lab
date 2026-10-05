"""Injected clocks. Tests advance time; nothing in the suite sleeps."""

from __future__ import annotations


class ManualClock:
    def __init__(self, start: int = 1_700_000_000) -> None:
        self.now = int(start)

    def time(self) -> int:
        return self.now

    def advance(self, seconds: int) -> int:
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self.now += int(seconds)
        return self.now
