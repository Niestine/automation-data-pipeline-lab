"""Injectable clock, sleeper, and structured JSON logger."""

from __future__ import annotations

from typing import Any, Optional, TextIO
import json
import time

from .models import LAB_NOW_MS


class ManualClock:
    def __init__(self, start_ms: int = LAB_NOW_MS) -> None:
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
    def __init__(self, stream: Optional[TextIO] = None, clock: Any = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.stream = stream
        self.clock = clock

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"event": event, **fields}
        if self.clock is not None and "ts_ms" not in record:
            record["ts_ms"] = self.clock.now_ms()
        self.events.append(record)
        if self.stream is not None:
            # ASCII escapes keep JSON Lines valid on consoles such as cp932
            # that cannot encode characters like "é".
            self.stream.write(json.dumps(record, ensure_ascii=True) + "\n")
        return record

    def of_type(self, event: str) -> list[dict[str, Any]]:
        return [item for item in self.events if item.get("event") == event]
