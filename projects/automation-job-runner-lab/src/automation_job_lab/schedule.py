"""Window alignment and a UTC minute/hour cron subset."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import ScheduleSpec


def window_start(now_ms: int, every_ms: int, offset_ms: int = 0) -> int:
    if every_ms <= 0:
        raise ValueError("every_ms must be positive")
    now_ms = int(now_ms)
    offset_ms = int(offset_ms)
    if now_ms < offset_ms:
        return offset_ms
    return ((now_ms - offset_ms) // every_ms) * every_ms + offset_ms


def interval_due(spec: ScheduleSpec, pipeline_window_ms: int) -> bool:
    """True when the pipeline window opens one of the job's own interval windows.

    The catalog loader requires every_ms/offset_ms to be multiples of the
    pipeline window, so e.g. every_ms = 2 * window_ms runs every other window.
    """
    if spec.every_ms is None:
        return True
    return window_start(pipeline_window_ms, spec.every_ms, spec.offset_ms) == pipeline_window_ms


def utc_parts(now_ms: int) -> datetime:
    return datetime.fromtimestamp(int(now_ms) / 1000.0, tz=timezone.utc)


def cron_matches(spec: ScheduleSpec, now_ms: int) -> bool:
    if spec.cron_minute is None:
        return True
    dt = utc_parts(now_ms)
    if dt.minute != spec.cron_minute:
        return False
    if spec.cron_hour is not None and dt.hour != spec.cron_hour:
        return False
    return True


def cron_due(spec: ScheduleSpec, last_success_ms: Optional[int], now_ms: int) -> bool:
    if not cron_matches(spec, now_ms):
        return False
    if last_success_ms is None:
        return True
    current_minute = (int(now_ms) // 60_000) * 60_000
    last_minute = (int(last_success_ms) // 60_000) * 60_000
    return last_minute < current_minute
