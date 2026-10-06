"""Seeded PCT campaigns. A hit is a classified failure, not a sleep.

``BoundExceeded`` is a setup error, not a hit: the PCT bound only holds for
runs inside ``n_max`` and ``k_budget``, so a run outside them stops the
campaign instead of inflating the rate.
"""

from __future__ import annotations

from yieldlab.engine import Scenario, Schedule, run
from yieldlab.errors import BoundExceeded, LabError


def pct_hits(
    scenario: Scenario,
    *,
    sample: int,
    mode: str,
    depth: int,
    k_budget: int,
    n_max: int,
    switch_interval: int = 1,
) -> int:
    if sample < 1:
        raise ValueError("sample must be a positive int")
    hits = 0
    for seed in range(sample):
        schedule = Schedule(
            mode=mode,
            seed=seed,
            depth=depth,
            n_max=n_max,
            k_budget=k_budget,
            switch_interval=switch_interval,
            policy="pct",
        )
        try:
            run(scenario, schedule)
        except BoundExceeded:
            raise
        except LabError:
            hits += 1
    return hits
