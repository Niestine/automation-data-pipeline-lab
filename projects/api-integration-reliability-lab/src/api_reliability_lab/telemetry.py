"""Injectable clock, sleeper, and in-memory structured logger."""

from __future__ import annotations

from typing import Any
import time


class ManualClock:
    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.t = int(start_ms)

    def now_ms(self) -> int:
        return self.t

    def now_s(self) -> int:
        return self.t // 1000

    def sleep(self, ms: int) -> None:
        self.t += max(0, int(ms))

    def advance_ms(self, ms: int) -> None:
        self.t += max(0, int(ms))


class WallClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def now_s(self) -> int:
        return int(time.time())

    def sleep(self, ms: int) -> None:
        time.sleep(max(0, int(ms)) / 1000.0)


class RecordingSleeper:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.delays: list[int] = []

    def __call__(self, ms: int) -> None:
        delay = int(ms)
        self.delays.append(delay)
        self.clock.sleep(delay)


class JsonLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"event": event, **fields}
        self.events.append(record)
        return record

    def of_type(self, event: str) -> list[dict[str, Any]]:
        return [item for item in self.events if item.get("event") == event]
