"""Full-jitter retry delay. Equal jitter and no-jitter exist only as test foils."""

from __future__ import annotations

import random
import re
from datetime import timezone
from email.utils import parsedate_to_datetime


def _temp(attempt: int, base: float, cap: float) -> float:
    # Attempt 1 is the first retry. Clamp the shift so the float stays finite.
    shift = min(int(attempt), 30)
    return min(float(cap), float(base) * float(2**shift))


def no_jitter(attempt: int, base: float = 1.0, cap: float = 300.0) -> float:
    return _temp(attempt, base, cap)


def full_jitter(
    attempt: int,
    rng: random.Random,
    base: float = 1.0,
    cap: float = 300.0,
) -> float:
    """Uniform draw from 0 through the capped exponential, inclusive of the cap."""
    return rng.uniform(0.0, _temp(attempt, base, cap))


def equal_jitter(
    attempt: int,
    rng: random.Random,
    base: float = 1.0,
    cap: float = 300.0,
) -> float:
    temp = _temp(attempt, base, cap)
    return temp / 2.0 + rng.uniform(0.0, temp / 2.0)


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Return a non-negative delay in seconds, or None when the header is absent or invalid."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return float(text)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - now)


def retry_delay(
    attempt: int,
    rng: random.Random,
    *,
    base: float = 1.0,
    cap: float = 300.0,
    retry_after_seconds: float | None = None,
) -> float:
    """Full jitter. A parsed Retry-After is a lower bound and jitter is added on top."""
    jitter = full_jitter(attempt, rng, base, cap)
    if retry_after_seconds is None:
        return jitter
    return float(retry_after_seconds) + jitter
