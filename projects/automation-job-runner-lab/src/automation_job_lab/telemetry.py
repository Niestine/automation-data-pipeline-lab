"""Injectable clock, sleeper, and structured JSON logger."""

from __future__ import annotations

from typing import Any, Optional, TextIO
import json
import time

from .models import LAB_EPOCH_MS


class ManualClock:
    def __init__(self, start_ms: int = LAB_EPOCH_MS) -> None:
        self.t = int(start_ms)

    def now_ms(self) -> int:
        return self.t

    def sleep(self, ms: int) -> None:
        self.t += max(0, int(ms))

    def advance_ms(self, ms: int) -> None:
        self.t += max(0, int(ms))


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
        delay = int(ms)
        self.delays.append(delay)
        self.clock.sleep(delay)


class JsonLogger:
    """Keeps events in memory and optionally writes each one as a JSON line."""

    def __init__(self, clock: Any = None, stream: Optional[TextIO] = None) -> None:
        self.clock = clock
        self.stream = stream
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {"event": event}
        if self.clock is not None:
            record["ts_ms"] = self.clock.now_ms()
        record.update(fields)
        self.events.append(record)
        if self.stream is not None:
            self.stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            self.stream.flush()
        return record

    def of_type(self, event: str) -> list[dict[str, Any]]:
        return [item for item in self.events if item.get("event") == event]
