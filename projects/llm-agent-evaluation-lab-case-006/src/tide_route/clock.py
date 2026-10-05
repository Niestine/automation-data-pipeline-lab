"""Manual clock so retry waits are asserted without sleeping the process."""

from __future__ import annotations


class FakeClock:
    def __init__(self) -> None:
        self.now_ms = 0.0
        self.sleeps: list[float] = []

    def sleep(self, delay_ms: float) -> None:
        waited = max(0.0, float(delay_ms))
        self.sleeps.append(waited)
        self.now_ms += waited
