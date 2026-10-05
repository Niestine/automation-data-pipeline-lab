"""Full-jitter backoff and a single-writer contention model.

The sleep formula is the full-jitter rule described by Marc Brooker
(AWS Architecture Blog, 2015): sleep is drawn from zero up to the capped
exponential delay. Brooker's call-count reduction is a result from his
simulator, not from this process. ``simulate_cohort`` is this lab's own
single-writer model and reports its own counts.
"""

from __future__ import annotations

import random
from typing import Mapping


def _ceiling(attempt: int, base: float, cap: float) -> float:
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    if base < 0 or cap < 0:
        raise ValueError("base and cap must be >= 0")
    return min(cap, base * (2 ** min(attempt, 16)))


def full_jitter(rng: random.Random, attempt: int, base: float, cap: float) -> float:
    """Return a delay in ``[0, min(cap, base * 2^attempt))``.

    A zero ceiling returns 0. The draw is the half-open uniform interval,
    so it can be zero and it does not exceed the cap.
    """
    ceiling = _ceiling(attempt, base, cap)
    if ceiling <= 0:
        return 0.0
    return rng.uniform(0.0, ceiling)


def synchronized_delay(attempt: int, base: float, cap: float) -> float:
    """Capped exponential delay with no jitter. Every client shares it."""
    return _ceiling(attempt, base, cap)


def jitter_lease_term(rng: random.Random, term_seconds: int, jitter_seconds: int) -> int:
    """Add ``0..jitter_seconds`` so a batch of grants does not share one expiry tick."""
    if term_seconds < 1:
        raise ValueError("term_seconds must be >= 1")
    if jitter_seconds < 0:
        raise ValueError("jitter_seconds must be >= 0")
    if jitter_seconds == 0:
        return term_seconds
    return term_seconds + rng.randrange(0, jitter_seconds + 1)


def simulate_cohort(
    mode: str,
    n_clients: int,
    n_jobs: int,
    seed: int,
    base: float = 1.0,
    cap: float = 32.0,
    service: float = 0.25,
) -> dict[str, float | int | str]:
    """Drain ``n_jobs`` through one writer.

    At each instant every client that is free and due competes. The lowest
    index wins and is busy for ``service``. The others count a failed claim
    and become due again after either a synchronized delay or full jitter.
    Time is simulated. No thread sleeps.
    """
    if mode not in {"full_jitter", "synchronized"}:
        raise ValueError("mode must be full_jitter or synchronized")
    if n_clients < 1 or n_jobs < 1:
        raise ValueError("n_clients and n_jobs must be >= 1")
    if service <= 0:
        raise ValueError("service must be > 0")
    rngs = [random.Random(seed + (i + 1) * 997) for i in range(n_clients)]
    next_ready = [0.0] * n_clients
    busy_until = [0.0] * n_clients
    attempt = [0] * n_clients
    failed = 0
    left = n_jobs
    now = 0.0
    last_finish = 0.0
    steps = 0
    limit = n_jobs * n_clients * 20 + 100
    while left > 0:
        steps += 1
        if steps > limit:
            raise RuntimeError("cohort simulation did not drain")
        ready = [
            i
            for i in range(n_clients)
            if busy_until[i] <= now + 1e-9 and next_ready[i] <= now + 1e-9
        ]
        if not ready:
            future = [t for t in next_ready + busy_until if t > now + 1e-9]
            now = min(future)
            continue
        winner = ready[0]
        for loser in ready[1:]:
            failed += 1
            if mode == "full_jitter":
                delay = full_jitter(rngs[loser], attempt[loser], base, cap)
            else:
                delay = synchronized_delay(attempt[loser], base, cap)
            attempt[loser] += 1
            next_ready[loser] = now + delay
        left -= 1
        attempt[winner] = 0
        busy_until[winner] = now + service
        next_ready[winner] = busy_until[winner]
        last_finish = busy_until[winner]
    return {
        "mode": mode,
        "failed_claims": failed,
        "drain_time": last_finish,
        "clients": n_clients,
        "jobs": n_jobs,
        "seed": seed,
    }


def compare_cohorts(
    n_clients: int = 20,
    n_jobs: int = 20,
    seed: int = 20261005,
) -> Mapping[str, dict[str, float | int | str]]:
    """Run both cohorts on the same job set. This lab's numbers, not Brooker's."""
    shared = {"n_clients": n_clients, "n_jobs": n_jobs, "seed": seed, "base": 1.0, "cap": 32.0, "service": 0.25}
    return {
        "full_jitter": simulate_cohort("full_jitter", **shared),
        "synchronized": simulate_cohort("synchronized", **shared),
    }
