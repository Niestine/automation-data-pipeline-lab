"""Planner/tool retry policy, seeded backoff, and a per-tool circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass, field
from random import Random
import hashlib


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_ms: int = 10
    max_delay_ms: int = 400
    multiplier: float = 2.0
    jitter_ms: int = 3


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_ms: int = 500
    failures: dict[str, int] = field(default_factory=dict)
    opened_at: dict[str, int] = field(default_factory=dict)

    def allow(self, tool: str, now_ms: int) -> bool:
        opened = self.opened_at.get(tool)
        if opened is None:
            return True
        if now_ms - opened >= self.cooldown_ms:
            del self.opened_at[tool]
            self.failures[tool] = 0
            return True
        return False

    def record_success(self, tool: str) -> None:
        self.failures[tool] = 0
        self.opened_at.pop(tool, None)

    def record_failure(self, tool: str, now_ms: int) -> None:
        self.failures[tool] = self.failures.get(tool, 0) + 1
        if self.failures[tool] >= self.failure_threshold:
            self.opened_at[tool] = now_ms

    def is_open(self, tool: str) -> bool:
        return tool in self.opened_at


NON_RETRYABLE = frozenset(
    {
        "blocked",
        "denied",
        "prompt_injection",
        "credential_request",
        "empty_goal",
        "external_exfiltration",
        "output_credential_leak",
        "unknown_tool",
        "capability_mismatch",
        "role_denied",
        "workspace_denied",
        "classification_denied",
        "forbidden_sequence",
        "duplicate_step",
        "bind_forward_ref",
        "bind_syntax",
        "bind_path",
        "bind_unresolved",
        "budget_exceeded",
        "step_limit",
        "tool_schema",
        "policy_denied",
        "no_script",
        "bad_script",
        "auth",
        "bad_request",
        "circuit_open",
        "slot_taken",
        "not_found",
        "not_submitted",
        "draft_provenance",
        "draft_conflict",
        "unknown_source",
    }
)

RETRYABLE_CODES = frozenset(
    {
        "parse_error",
        "schema_error",
        "timeout",
        "provider_error",
        "transient_io",
    }
)


def is_retryable(code: str, *, transient: bool, policy: RetryPolicy) -> bool:
    if policy.max_attempts <= 1:
        return False
    if code in NON_RETRYABLE:
        return False
    if transient:
        return True
    return code in RETRYABLE_CODES


def rng_for(packet_id: str, seed: int) -> Random:
    material = f"{seed}:{packet_id}".encode("utf-8")
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
