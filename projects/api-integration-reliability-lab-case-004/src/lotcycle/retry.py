"""Retry classification and the three capped jitter arms.

The exponent is clamped at 62 so a long attempt count cannot overflow a
float. Attempts 0 through 4, which the formula tests use, are unchanged.
"""

from __future__ import annotations

import random

from lotcycle.params import (
    ACCESS_LIFETIME,
    JITTER_BASE,
    JITTER_CAP,
    RETRIABLE_STATUS,
    TERMINAL_ERRORS,
)


def _ceiling(attempt: int, base: float, cap: float) -> float:
    power = attempt if attempt < 62 else 62
    return min(cap, base * (2**power))


def full_jitter(
    rng: random.Random,
    attempt: int,
    base: float = JITTER_BASE,
    cap: float = JITTER_CAP,
) -> float:
    """sleep = random(0, min(cap, base * 2^attempt))."""
    return rng.uniform(0.0, _ceiling(attempt, base, cap))


def equal_jitter(
    rng: random.Random,
    attempt: int,
    base: float = JITTER_BASE,
    cap: float = JITTER_CAP,
) -> float:
    """Half of the capped backoff, plus a uniform draw over the other half."""
    temp = _ceiling(attempt, base, cap)
    return temp / 2.0 + rng.uniform(0.0, temp / 2.0)


def decorrelated_jitter(
    rng: random.Random,
    previous: float,
    base: float = JITTER_BASE,
    cap: float = JITTER_CAP,
) -> float:
    """sleep = min(cap, random(base, previous * 3)). previous starts at base."""
    upper = previous * 3.0
    if upper < base:
        upper = base
    return min(cap, rng.uniform(base, upper))


def no_jitter(attempt: int, base: float = JITTER_BASE, cap: float = JITTER_CAP) -> float:
    """Deterministic capped exponential, used only as the comparison arm."""
    return _ceiling(attempt, base, cap)


def classify_token_http(status: int, error: str | None) -> str:
    """Return 'success', 'retry', or 'terminal'.

    HTTP 500/502/503/504 are retried only because the lab server rolls the
    rotation transaction back before those statuses. A classified OAuth
    error is terminal even if a future caller wraps it in a 5xx.
    """
    if status == 200:
        return "success"
    if error in TERMINAL_ERRORS:
        return "terminal"
    if status in RETRIABLE_STATUS:
        return "retry"
    return "terminal"


def proactive_delay(
    rng: random.Random | None,
    lifetime: float = ACCESS_LIFETIME,
    randomize: bool = True,
) -> float:
    """Delay until the next proactive refresh.

    The zero-random arm returns the access-token lifetime, so clients that
    were issued together share expires_at. The randomized arm draws
    uniformly from [0.5T, 1.5T].
    """
    if not randomize or rng is None:
        return float(lifetime)
    return rng.uniform(0.5 * lifetime, 1.5 * lifetime)


def proactive_refresh_time(
    issued_at: float,
    rng: random.Random | None,
    lifetime: float = ACCESS_LIFETIME,
    randomize: bool = True,
) -> float:
    return issued_at + proactive_delay(rng, lifetime, randomize)


def resume_after_outage(
    release_at: float,
    rng: random.Random | None,
    lifetime: float = ACCESS_LIFETIME,
    randomize: bool = True,
) -> float:
    """Next token attempt after a shared outage.

    Without randomization every waiter fires on the release tick. With
    randomization the wait is the same [0.5T, 1.5T] draw, added to release.
    """
    if not randomize or rng is None:
        return float(release_at)
    return float(release_at) + proactive_delay(rng, lifetime, True)


def simulate_contention(
    policy: str,
    n: int = 32,
    service_ms: int = 10,
    seed: int = 1,
    base: float = 50.0,
    cap: float = 2000.0,
) -> dict:
    """One in-flight slot. Clients that arrive together retry until success.

    A computed sleep below 1 ms is scheduled 1 ms later so a zero full-jitter
    draw cannot spin the event loop. The formula tests call the jitter
    functions directly and do not use this clamp.
    """
    import heapq

    heap: list[tuple[int, int, int]] = []
    seq = 0
    for i in range(n):
        heapq.heappush(heap, (0, seq, i))
        seq += 1
    done = [False] * n
    attempt = [0] * n
    previous = [float(base)] * n
    rngs = [random.Random(seed + i * 17) for i in range(n)]
    server_free = 0
    calls = 0
    finish = 0
    while heap and not all(done):
        now, _, client = heapq.heappop(heap)
        if done[client]:
            continue
        calls += 1
        if calls > 200_000:
            break
        if now >= server_free:
            server_free = now + service_ms
            done[client] = True
            finish = server_free
            continue
        delay, previous[client] = _contention_delay(
            policy, rngs[client], attempt[client], previous[client], base, cap
        )
        attempt[client] += 1
        if delay < 1:
            delay = 1
        seq += 1
        heapq.heappush(heap, (now + delay, seq, client))
    return {
        "calls": calls,
        "finish_ms": finish,
        "done": sum(done),
        "policy": policy,
    }


def _contention_delay(
    policy: str,
    rng: random.Random,
    attempt: int,
    previous: float,
    base: float,
    cap: float,
) -> tuple[int, float]:
    if policy == "none":
        sleep = no_jitter(attempt, base, cap)
        return int(sleep), previous
    if policy == "full":
        sleep = full_jitter(rng, attempt, base, cap)
        return int(sleep), previous
    if policy == "equal":
        sleep = equal_jitter(rng, attempt, base, cap)
        return int(sleep), previous
    if policy == "decorr":
        sleep = decorrelated_jitter(rng, previous, base, cap)
        return int(sleep), sleep
    raise ValueError(policy)
