"""Retry token bucket, independent of the dollar ledger.

Standard-mode numbers: capacity 500, transient retry 14, throttling retry 5,
first-try success restores 1 up to capacity, a successful retry restores only
its own cost. An empty bucket fails the retry with no sleep. The first attempt
is never delayed. Adaptive mode is not implemented.

Full-jitter delay is U(0, 1) * min(20000 ms, base * 2^retry), retry starting
at 0, base 50 ms transient and 1000 ms throttling. An integer Retry-After is a
minimum wait and is not passed through an AWS retry-after clamp.
"""

from __future__ import annotations

CAPACITY = 500
TRANSIENT_COST = 14
THROTTLE_COST = 5
TRANSIENT_BASE_MS = 50.0
THROTTLE_BASE_MS = 1000.0
DELAY_CAP_MS = 20000.0


class RetryBucket:
    def __init__(self, balance: int = CAPACITY, capacity: int = CAPACITY) -> None:
        self.balance = balance
        self.capacity = capacity

    def cost_of(self, kind: str) -> int:
        if kind == "transient":
            return TRANSIENT_COST
        if kind == "throttling":
            return THROTTLE_COST
        raise ValueError(f"unknown retry kind: {kind}")

    def can_pay(self, kind: str) -> bool:
        return self.balance >= self.cost_of(kind)

    def charge(self, kind: str) -> int:
        cost = self.cost_of(kind)
        if self.balance < cost:
            raise RuntimeError("retry bucket cannot pay")
        self.balance -= cost
        return cost

    def on_first_try_success(self) -> None:
        self.balance = min(self.capacity, self.balance + 1)

    def on_retry_success(self, cost: int) -> None:
        self.balance = min(self.capacity, self.balance + cost)


def full_jitter_ms(rng, retry_index: int, kind: str) -> float:
    base = THROTTLE_BASE_MS if kind == "throttling" else TRANSIENT_BASE_MS
    cap = min(DELAY_CAP_MS, base * (2 ** retry_index))
    return float(rng.random()) * cap


def retry_wait_ms(
    rng,
    retry_index: int,
    kind: str,
    retry_after_seconds: int | None,
    parsed: str,
) -> tuple[float, str]:
    """Return (delay_ms, parse_flag). Non-integer Retry-After stays on the jitter path."""
    jitter = full_jitter_ms(rng, retry_index, kind)
    if parsed == "integer" and retry_after_seconds is not None:
        # Vendor minimum. Not clamped to DELAY_CAP_MS.
        return max(jitter, float(retry_after_seconds) * 1000.0), "integer"
    return jitter, "absent_or_unparsed"
