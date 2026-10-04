"""Retry policy with deterministic exponential backoff."""

from __future__ import annotations

from dataclasses import dataclass
from random import Random
import hashlib


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_ms: int = 10
    max_delay_ms: int = 400
    multiplier: float = 2.0
    jitter_ms: int = 3


NON_RETRYABLE = frozenset(
    {
        "blocked",
        "denied",
        "prompt_injection",
        "credential_request",
        "bulk_pii_export",
        "external_exfiltration",
        "empty_ticket",
        "output_credential_leak",
        "unknown_action",
        "policy_violation",
        "no_script",
        "bad_script",
        "auth",
        "bad_request",
    }
)


def is_retryable(code: str, *, transient: bool, policy: RetryPolicy) -> bool:
    if policy.max_attempts <= 1:
        return False
    if code in NON_RETRYABLE:
        return False
    if transient:
        return True
    return code in {"parse_error", "schema_error", "timeout", "provider_error"}


def rng_for(ticket_id: str, seed: int) -> Random:
    material = f"{seed}:{ticket_id}".encode("utf-8")
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
