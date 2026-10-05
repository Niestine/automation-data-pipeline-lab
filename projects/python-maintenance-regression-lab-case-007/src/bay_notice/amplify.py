"""Retry amplification under a constant independent failure probability.

The closed form is an expectation, not a measurement of a correlated outage.
"""

from __future__ import annotations

import itertools
import random

from .budget import ReplayRandom, SharedBudget


def expected_raf(failure_probability: float, retries: int) -> float:
    """(1 - p^(n+1)) / (1 - p). At p == 1 the sum is n+1."""

    if retries < 0:
        raise ValueError("retries must be >= 0")
    probability = float(failure_probability)
    if probability < 0 or probability > 1:
        raise ValueError("failure probability must be in [0, 1]")
    if probability == 1:
        return float(retries + 1)
    if probability == 0:
        return 1.0
    return (1.0 - probability ** (retries + 1)) / (1.0 - probability)


def attempt_count(rng: random.Random, failure_probability: float, retries: int) -> int:
    made = 0
    while True:
        made += 1
        failed = rng.random() < failure_probability
        if not failed or made == retries + 1:
            return made


def terminal_calls(
    rng: random.Random,
    failure_probability: float,
    retries: int,
    depth: int,
) -> int:
    """Each tier draws its own attempt count and multiplies the next tier."""

    if depth < 1:
        raise ValueError("depth must be >= 1")
    made = attempt_count(rng, failure_probability, retries)
    if depth == 1:
        return made
    return sum(
        terminal_calls(rng, failure_probability, retries, depth - 1) for _ in range(made)
    )


def simulate_raf(
    jobs: int,
    failure_probability: float,
    retries: int,
    depth: int,
    seed: int,
) -> float:
    rng = random.Random(seed)
    total = sum(
        terminal_calls(rng, failure_probability, retries, depth) for _ in range(jobs)
    )
    return total / jobs


def tier_calls(depth: int, attempts: int, budget: SharedBudget | None = None) -> int:
    """Gate calls when every call fails and each tier re-issues its downstream call.

    Without a budget every tier spends all `attempts`. With one shared budget each
    failure reports OVERLOADED, and a refused admission ends that tier's loop.
    """

    if depth < 1 or attempts < 1:
        raise ValueError("depth and attempts must be >= 1")
    calls = 0
    for attempt in range(attempts):
        calls += 1 if depth == 1 else tier_calls(depth - 1, attempts, budget)
        if attempt + 1 == attempts:
            break
        if budget is not None:
            budget.observe(True)
            budget.note_status("OVERLOADED")
            budget.tick()
            if not budget.try_admit():
                break
    return calls


def always_fail_product(depth: int, attempts: int) -> int:
    """Uncoordinated tiers that all fail. Each tier issues `attempts` calls downward."""

    return tier_calls(depth, attempts)


def shared_overloaded_calls(depth: int = 3, attempts: int = 4) -> int:
    """Same walk with one shared budget. OVERLOADED refuses every tier's retry."""

    budget = SharedBudget(base_load=1, rng=ReplayRandom([0.0] * attempts * depth))
    return tier_calls(depth, attempts, budget)


def exact_half_load(retries: int = 3) -> tuple[int, int]:
    """Enumerate every equally likely fair-coin outcome sequence for one job.

    Each sequence of retries + 1 coin flips is one job. The attempt count comes
    from `attempt_count`, so the table is derived, not typed in.
    """

    if retries < 0:
        raise ValueError("retries must be >= 0")
    calls = 0
    jobs = 0
    for flips in itertools.product((True, False), repeat=retries + 1):
        draws = [0.0 if failed else 0.75 for failed in flips]
        calls += attempt_count(ReplayRandom(draws), 0.5, retries)  # type: ignore[arg-type]
        jobs += 1
    return calls, jobs
