"""Retry classification and deterministic exponential backoff."""

from __future__ import annotations

from dataclasses import dataclass
from random import Random
import hashlib

from .errors import PermanentError, RetryableError, SchemaError, SimulatedCrash
from .models import RetrySpec


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_ms: int = 10
    max_delay_ms: int = 200
    multiplier: float = 2.0
    jitter_ms: int = 3

    @classmethod
    def from_spec(cls, spec: RetrySpec) -> RetryPolicy:
        return cls(
            max_attempts=spec.max_attempts,
            base_delay_ms=spec.base_delay_ms,
            max_delay_ms=spec.max_delay_ms,
            multiplier=spec.multiplier,
            jitter_ms=spec.jitter_ms,
        )


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, SimulatedCrash):
        return False
    if isinstance(exc, (PermanentError, SchemaError)):
        return False
    return isinstance(exc, RetryableError)


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
