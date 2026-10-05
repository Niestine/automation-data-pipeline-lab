"""Capped exponential backoff. Full jitter is the client default."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .errors import ResponseDropped
from .httpmsg import Response
from .journal import Journal


def expo(base: float, cap: float, attempt: int) -> float:
    power = attempt if attempt < 62 else 62
    return min(cap, base * (2**power))


def full_jitter(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """uniform(0, min(cap, base * 2^attempt)). Matches the archived simulator."""
    return rng.uniform(0, expo(base, cap, attempt))


def equal_jitter(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    value = expo(base, cap, attempt)
    return value / 2 + rng.uniform(0, value / 2)


class DecorrelatedJitter:
    def __init__(self, base: float, cap: float, rng: random.Random) -> None:
        self.base = base
        self.cap = cap
        self.rng = rng
        self.sleep = base

    def delay(self, _attempt: int) -> float:
        self.sleep = min(self.cap, self.rng.uniform(self.base, self.sleep * 3))
        return self.sleep


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base: float = 0.05
    cap: float = 2.0


# 409 is retried unchanged. 412 and 422 are not.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
NO_RETRY_STATUSES = frozenset({400, 401, 403, 412, 422})


def parse_retry_after(headers: dict[str, str]) -> float | None:
    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value.strip()
            break
    if raw is None:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return float(raw)
    return None


def call_with_retry(
    operation,
    policy: RetryPolicy,
    rng: random.Random,
    sleeper,
    journal: Journal,
):
    """Call `operation` until it returns a final response or the budget ends."""
    attempt = 0
    while True:
        try:
            response: Response = operation()
        except ResponseDropped:
            decision = (
                "surface_last_error"
                if attempt + 1 >= policy.max_attempts
                else "retry_same_request"
            )
            journal.record("client_timeout", decision, attempt=attempt)
            if attempt + 1 >= policy.max_attempts:
                raise
            sleeper(full_jitter(attempt, policy.base, policy.cap, rng))
            attempt += 1
            continue
        status = response.status
        if status in NO_RETRY_STATUSES:
            journal.record(f"client_status_{status}", "do_not_retry", attempt=attempt)
            return response
        if status in RETRY_STATUSES:
            retry_after = parse_retry_after(response.headers)
            if attempt + 1 >= policy.max_attempts:
                journal.record(
                    f"client_status_{status}", "surface_last_error", attempt=attempt
                )
                return response
            if retry_after is not None:
                wait = retry_after
                decision = "wait_retry_after"
            else:
                wait = full_jitter(attempt, policy.base, policy.cap, rng)
                decision = "full_jitter_retry"
            journal.record(f"client_status_{status}", decision, attempt=attempt, wait=wait)
            sleeper(wait)
            attempt += 1
            continue
        return response
