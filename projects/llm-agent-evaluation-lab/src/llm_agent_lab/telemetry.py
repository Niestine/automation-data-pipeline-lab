"""Structured in-memory telemetry used by the orchestrator and tests."""

from __future__ import annotations

from typing import Any
import time


class ManualClock:
    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.t = int(start_ms)

    def now_ms(self) -> int:
        return self.t

    def sleep(self, ms: int) -> None:
        self.t += int(ms)


class WallClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def sleep(self, ms: int) -> None:
        time.sleep(max(0, int(ms)) / 1000.0)


class RecordingSleeper:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.delays: list[int] = []

    def __call__(self, ms: int) -> None:
        self.delays.append(int(ms))
        self.clock.sleep(ms)


class JsonLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"event": event, **fields}
        self.events.append(record)
        return record
