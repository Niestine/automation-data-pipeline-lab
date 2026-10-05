"""Refresh scores and the fixed-budget policy comparison.

The default score spends the non-audit budget where a sync buys freshness over
the horizon. Uniform and proportional selection are baselines. Proportional
selection is not the default: a page that will change many times before the
next affordable visit scores near zero.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from incremental_crawl_lab.estimate import expected_age, expected_freshness, time_average_freshness
from incremental_crawl_lab.metrics import time_average


@dataclass(frozen=True)
class SchedItem:
    key: str
    lam: float
    synced_at: float
    weight: float = 1.0


def audit_count(budget: int) -> int:
    if budget <= 0:
        return 0
    return max(1, budget // 10)


def score_freshness(lam: float, tau: float, horizon: float, weight: float) -> float:
    """w * (1 - exp(-λ τ)) * (1 - exp(-λ T)) / (λ T). Zero when λ is 0 or τ is 0."""
    if lam <= 0 or tau <= 0 or horizon <= 0 or weight <= 0:
        return 0.0
    stale = 1.0 - expected_freshness(lam, tau)
    return weight * stale * time_average_freshness(lam, horizon)


def score_age(lam: float, tau: float, weight: float) -> float:
    """w * (τ - (1 - exp(-λ τ)) / λ) when λ > 0, else 0. That is w times the expected age."""
    if lam <= 0 or tau <= 0 or weight <= 0:
        return 0.0
    return weight * expected_age(lam, tau)


def weighted_sample(
    rng: random.Random,
    items: list[SchedItem],
    k: int,
) -> list[SchedItem]:
    pool = [item for item in items if item.lam > 0]
    chosen: list[SchedItem] = []
    while pool and len(chosen) < k:
        total = sum(item.lam for item in pool)
        if total <= 0:
            break
        draw = rng.random() * total
        acc = 0.0
        pick_at = 0
        for index, item in enumerate(pool):
            acc += item.lam
            if acc >= draw:
                pick_at = index
                break
        chosen.append(pool.pop(pick_at))
    return chosen


def choose_batch(
    items: list[SchedItem],
    budget: int,
    objective: str,
    rng: random.Random,
    now: float,
    horizon: float,
) -> list[SchedItem]:
    """Pick up to ``budget`` items. The oldest syncs fill the audit floor first."""
    if budget <= 0 or not items:
        return []
    slots = min(budget, len(items))
    floor = min(audit_count(budget), slots)
    oldest = sorted(items, key=lambda item: (item.synced_at, item.key))
    chosen = list(oldest[:floor])
    chosen_keys = {item.key for item in chosen}
    pool = [item for item in items if item.key not in chosen_keys]
    rest = slots - len(chosen)
    if rest <= 0 or not pool:
        return chosen
    if objective == "uniform":
        picked = rng.sample(pool, min(rest, len(pool)))
    elif objective == "proportional":
        picked = weighted_sample(rng, pool, rest)
    elif objective == "age":
        picked = sorted(
            pool,
            key=lambda item: (
                -score_age(item.lam, max(0.0, now - item.synced_at), item.weight),
                item.key,
            ),
        )[:rest]
    elif objective == "freshness":
        picked = sorted(
            pool,
            key=lambda item: (
                -score_freshness(
                    item.lam,
                    max(0.0, now - item.synced_at),
                    horizon,
                    item.weight,
                ),
                item.key,
            ),
        )[:rest]
    else:
        raise ValueError(f"unknown objective {objective}")
    return chosen + picked


def _poisson_times(
    rng: random.Random,
    lam: float,
    start: float,
    end: float,
) -> list[float]:
    if lam <= 0:
        return []
    times: list[float] = []
    cursor = start
    while True:
        cursor += rng.expovariate(lam)
        if cursor >= end:
            break
        times.append(cursor)
    return times


def simulate_policies(
    *,
    seed: int = 20261005,
    weeks: int = 8,
    horizon_days: float = 7.0,
    budget: int = 25,
    mix: tuple[tuple[float, int], ...] = ((0.01, 50), (0.1, 30), (5.0, 20)),
) -> dict[str, dict[str, float]]:
    """Eight weekly budgets on a collection that is already one horizon old.

    Rates are per day. The change log is shared. Each policy has its own
    random stream seeded from ``seed`` so the comparison does not depend on
    call order.
    """
    keys: list[str] = []
    lams: list[float] = []
    for lam, count in mix:
        for index in range(count):
            keys.append(f"{lam:.2f}:{index:03d}")
            lams.append(lam)
    t_end = weeks * horizon_days
    initial = -horizon_days
    change_rng = random.Random(seed)
    changes = [
        _poisson_times(change_rng, lam, initial, t_end) for lam in lams
    ]
    results: dict[str, dict[str, float]] = {}
    for objective in ("uniform", "proportional", "freshness", "age"):
        policy_rng = random.Random(seed)
        synced = [initial] * len(keys)
        sync_lists = [[initial] for _ in keys]
        for week in range(weeks):
            now = week * horizon_days
            batch = [
                SchedItem(keys[i], lams[i], synced[i], 1.0) for i in range(len(keys))
            ]
            picked = {
                item.key
                for item in choose_batch(
                    batch, budget, objective, policy_rng, now, horizon_days
                )
            }
            for index, key in enumerate(keys):
                if key in picked:
                    synced[index] = now
                    sync_lists[index].append(now)
        fresh: list[float] = []
        age: list[float] = []
        for index in range(len(keys)):
            freshness, age_value = time_average(sync_lists[index], changes[index], 0.0, t_end)
            fresh.append(freshness)
            age.append(age_value)
        results[objective] = {
            "freshness": sum(fresh) / len(fresh),
            "age": sum(age) / len(age),
        }
    return results
