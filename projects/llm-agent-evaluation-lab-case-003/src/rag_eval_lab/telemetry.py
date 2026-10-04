"""Manual clock, recording sleeper, and structured events. Nothing here blocks."""

from __future__ import annotations

from typing import Any


class ManualClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now


class RecordingSleeper:
    """Records delays and advances a manual clock. Does not block."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        delay = float(seconds)
        self.delays.append(delay)
        self.clock.now += delay


class JsonLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event(self, name: str, **fields: Any) -> None:
        self.events.append({"event": name, **fields})
