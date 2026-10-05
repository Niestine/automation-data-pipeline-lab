"""Retry delays. The multi-day table uses bounded jitter, not full jitter.

Full jitter is ``randrange(0, min(cap, base * 2^attempt) + 1)``. Drawing
that over a 24 hour slot would collapse the schedule, so the publisher uses
it only for a shared 429/502/503/504, within a separate attempt budget.
"""

from __future__ import annotations

from .horizons import SCHEDULE_SECONDS


def bounded_slot_delay(slot: int, rng) -> int:
    """Stay inside the slot. A 24 hour slot cannot draw a sub-second sleep."""

    if slot <= 0:
        return 0
    span = max(1, slot // 5)
    return slot - rng.randrange(0, span + 1)


def full_jitter_delay(attempt: int, rng, *, base: int, cap: int) -> int:
    if attempt < 0 or base < 0 or cap < 0:
        raise ValueError("jitter parameters must be non-negative")
    ceiling = min(cap, base * (2**attempt))
    return rng.randrange(0, ceiling + 1)


def no_jitter_delay(attempt: int, *, base: int, cap: int) -> int:
    """Synchronized backoff. Identical across clients that failed together."""

    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    return min(cap, base * (2**attempt))


def parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text.isdigit():
        return None
    return int(text)


def next_slot(attempt_just_finished: int) -> int | None:
    """Table delay for the attempt after ``attempt_just_finished`` (0-based)."""

    upcoming = attempt_just_finished + 1
    if upcoming >= len(SCHEDULE_SECONDS):
        return None
    return SCHEDULE_SECONDS[upcoming]
