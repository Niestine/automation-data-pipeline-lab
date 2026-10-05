"""Runner configuration. Deadlines are applied with the database clock."""

from __future__ import annotations

from dataclasses import dataclass

from shiftlease.contract import DEFAULT_RETENTION_SECONDS


@dataclass(frozen=True)
class Config:
    lease_term_seconds: int = 4
    heartbeat_seconds: int = 2
    clock_uncertainty_seconds: int = 1
    lease_jitter_seconds: int = 1
    busy_timeout_ms: int = 4000
    jitter_base_seconds: float = 0.05
    jitter_cap_seconds: float = 1.0
    effect_retry_cap: int = 2
    max_attempts: int = 5
    retention_seconds: int = DEFAULT_RETENTION_SECONDS
    synchronous: str = "FULL"

    def __post_init__(self) -> None:
        if self.lease_term_seconds < 1:
            raise ValueError("lease_term_seconds must be >= 1")
        if self.clock_uncertainty_seconds < 0:
            raise ValueError("clock_uncertainty_seconds must be >= 0")
        if self.clock_uncertainty_seconds >= self.lease_term_seconds:
            raise ValueError("clock_uncertainty_seconds must be < lease_term_seconds")
        if self.lease_jitter_seconds < 0:
            raise ValueError("lease_jitter_seconds must be >= 0")
        if self.heartbeat_seconds < 0:
            raise ValueError("heartbeat_seconds must be >= 0")
        if self.heartbeat_seconds and self.heartbeat_seconds * 2 > self.lease_term_seconds:
            raise ValueError("heartbeat_seconds must be at most half the lease term")
        if self.busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be >= 0")
        if self.jitter_base_seconds < 0 or self.jitter_cap_seconds < 0:
            raise ValueError("jitter base and cap must be >= 0")
        if self.jitter_cap_seconds < self.jitter_base_seconds:
            raise ValueError("jitter_cap_seconds must be >= jitter_base_seconds")
        if self.effect_retry_cap < 0:
            raise ValueError("effect_retry_cap must be >= 0")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.retention_seconds < 1:
            raise ValueError("retention_seconds must be >= 1")
        if self.synchronous not in {"FULL", "NORMAL"}:
            raise ValueError("synchronous must be FULL or NORMAL")

    @property
    def max_lease_term_seconds(self) -> int:
        """Longest deadline this process will grant, including lease jitter."""
        return self.lease_term_seconds + self.lease_jitter_seconds
