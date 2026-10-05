"""Retry predicate, attempt cap, and delay jitter.

Jitter here only keeps independent desks off the same timestamps.
The floor is applied after the sample. This is not a named full-jitter formula.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .errors import OracleFailure, TransientGateError


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    floor_s: float = 1.0
    ceiling_s: float = 60.0
    granularity_s: float = 0.05
    multiplier: float = 2.0
    alpha: float = 0.125
    beta: float = 0.25
    k: float = 4.0
    reset_after: int | None = None
    retryable: frozenset[type[BaseException]] = field(
        default_factory=lambda: frozenset({TransientGateError})
    )
    jitter: str = "fixed"
    seed: int = 0
    spread: float = 0.25

    def allows(self, error: BaseException) -> bool:
        return type(error) in self.retryable


def tcp_6298_policy(**overrides: object) -> RetryPolicy:
    """RFC 6298 shape: 1s floor and a ceiling of at least 60s."""

    values: dict[str, object] = {
        "floor_s": 1.0,
        "ceiling_s": 60.0,
        "granularity_s": 0.05,
        "multiplier": 2.0,
        "alpha": 0.125,
        "beta": 0.25,
        "k": 4.0,
    }
    values.update(overrides)
    return RetryPolicy(**values)  # type: ignore[arg-type]


def application_policy(**overrides: object) -> RetryPolicy:
    """Sub-second floor. More aggressive than RFC 6298 allows for TCP."""

    values: dict[str, object] = {
        "floor_s": 0.05,
        "ceiling_s": 1.6,
        "granularity_s": 0.001,
        "multiplier": 2.0,
        "alpha": 0.125,
        "beta": 0.25,
        "k": 4.0,
    }
    values.update(overrides)
    return RetryPolicy(**values)  # type: ignore[arg-type]


class JitterSampler:
    def __init__(self, mode: str = "fixed", seed: int = 0, spread: float = 0.25) -> None:
        if mode not in {"fixed", "seeded"}:
            raise ValueError(f"unknown jitter mode {mode!r}")
        self.mode = mode
        self.spread = spread
        self._rng = random.Random(seed)

    def factor(self) -> float:
        if self.mode == "fixed":
            return 1.0
        return 1.0 + self._rng.uniform(-self.spread, self.spread)


class SequenceSampler:
    """Test double. Each factor() pops the next published sample."""

    def __init__(self, factors: list[float]) -> None:
        self._factors = list(factors)

    def factor(self) -> float:
        if not self._factors:
            raise RuntimeError("jitter sample underrun")
        return self._factors.pop(0)


def apply_jitter(base: float, floor_s: float, ceiling_s: float, factor: float) -> float:
    delayed = base * factor
    if floor_s > 0:
        delayed = max(floor_s, delayed)
    if ceiling_s > 0:
        delayed = min(ceiling_s, delayed)
    return delayed


def synchronized_delays(sequences: list[list[float]]) -> bool:
    if not sequences:
        return False
    head = sequences[0]
    return all(item == head for item in sequences)


def assert_cap(calls: int, max_retries: int) -> None:
    """Fail when executions continue past the original attempt plus max_retries."""

    if calls > max_retries + 1:
        raise OracleFailure(f"cap exceeded: {calls} calls for max_retries={max_retries}")


def assert_positive_delays(waits: list[float]) -> None:
    """Fail when two attempts of one notice have no delay between them."""

    if not waits or any(wait <= 0 for wait in waits):
        raise OracleFailure(f"missing delay: {waits}")


def assert_every_send_decided(transmissions: int, decisions: list[str]) -> None:
    """Fail when a transmission ended without a logged handling decision.

    An empty or log-only handler turns an injected error into success and leaves
    no decision behind, so the count of decisions falls short of the sends.
    """

    if len(decisions) != transmissions:
        raise OracleFailure(f"{transmissions} sends but {len(decisions)} decisions: {decisions}")


def assert_desynchronized(sequences: list[list[float]]) -> None:
    if synchronized_delays(sequences):
        raise OracleFailure("clients share one delay sequence")
