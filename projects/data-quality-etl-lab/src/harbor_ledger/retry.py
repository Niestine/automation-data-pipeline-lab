"""Bounded read retries with the delays supplied by the caller.

The sleeper is injected so tests can record the schedule without waiting.
The demo profile uses millisecond delays; production callers may pass ``time.sleep``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


def read_with_retry(
    path: str | Path,
    attempts: int,
    delays_ms: list[int],
    reader: Callable[[str], bytes] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> bytes:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    read = reader or (lambda item: Path(item).read_bytes())
    pause = sleeper or (lambda _seconds: None)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return read(str(path))
        except OSError as exc:
            last = exc
            if attempt + 1 == attempts:
                break
            delay = delays_ms[min(attempt, len(delays_ms) - 1)] if delays_ms else 0
            pause(delay / 1000)
    assert last is not None
    raise last
