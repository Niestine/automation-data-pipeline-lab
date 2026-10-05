"""Injectable clock. The lab never sleeps on the wall clock."""

from __future__ import annotations

from datetime import datetime, timezone


DEMO_START = datetime(2026, 10, 5, tzinfo=timezone.utc)


def demo_start_seconds() -> float:
    return DEMO_START.timestamp()


class ManualClock:
    """Monotonic virtual clock. ``sleep`` only advances ``now``."""

    def __init__(self, now: float | None = None) -> None:
        self.now = float(demo_start_seconds() if now is None else now)

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        self.now += float(seconds)
