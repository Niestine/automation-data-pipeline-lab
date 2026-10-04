"""Retry classification and deterministic exponential backoff."""

from __future__ import annotations

from dataclasses import dataclass
from random import Random
import hashlib
import re

from .errors import TransportError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_ms: int = 10
    max_delay_ms: int = 200
    multiplier: float = 2.0
    jitter_ms: int = 3
    # Upper bound on a server-supplied Retry-After so a bad header cannot stall the job.
    max_retry_after_ms: int = 30_000


RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS


def is_retryable_request(method: str, headers: dict[str, str]) -> bool:
    """Unsafe methods retry only when an Idempotency-Key is present."""
    if method.upper() in SAFE_METHODS:
        return True
    return bool(headers.get("idempotency-key"))


def is_retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, TransportError):
        return exc.code in {"timeout", "connection", "reset"}
    return False


def rng_for(*parts: object) -> Random:
    material = ":".join(str(part) for part in parts).encode("utf-8")
    n = int(hashlib.sha256(material).hexdigest()[:16], 16)
    return Random(n)


def backoff_ms(policy: RetryPolicy, attempt: int, rng: Random) -> int:
    """Delay after a failed attempt. `attempt` is 1-based."""
    power = max(0, attempt - 1)
    raw = int(policy.base_delay_ms * (policy.multiplier ** power))
    capped = min(policy.max_delay_ms, raw)
    if policy.jitter_ms > 0:
        capped += rng.randint(0, policy.jitter_ms)
    return capped


def _ascii_int(value: str | None) -> int | None:
    # str.isdigit() accepts characters such as "²" that int() rejects.
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,12}", value) is None:
        return None
    return int(value)


def delay_from_headers(headers: dict[str, str]) -> int | None:
    """Honor Retry-After (delta-seconds) or X-Retry-After-Ms when present.

    HTTP-date Retry-After values are not parsed; callers fall back to backoff.
    """
    ms_value = _ascii_int(headers.get("x-retry-after-ms"))
    if ms_value is not None:
        return ms_value
    seconds = _ascii_int(headers.get("retry-after"))
    if seconds is not None:
        return seconds * 1000
    return None
