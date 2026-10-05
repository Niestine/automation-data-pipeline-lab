"""Virtual clock. Tests advance it; the desk never sleeps on the wall clock."""

from __future__ import annotations


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def sleep(self, delay: float) -> None:
        if delay < 0:
            raise ValueError("delay must be >= 0")
        amount = float(delay)
        self.waits.append(amount)
        self.now += amount
