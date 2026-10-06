"""Shared constructors for the yield-lab tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yieldlab.engine import Scenario, Schedule, run  # noqa: E402

PROJECT = ROOT
REGRESSIONS = PROJECT / "regressions"


def scenario(name: str, threads: dict, **kwargs) -> Scenario:
    kwargs.setdefault("variant", "case")
    kwargs.setdefault("invariant_id", name)
    return Scenario(name=name, threads=threads, **kwargs)


def execute(case: Scenario, **kwargs):
    params = {
        "mode": "free",
        "n_max": 8,
        "k_budget": 64,
        "switch_interval": 1,
        "policy": "pct",
    }
    params.update(kwargs)
    return run(case, Schedule(**params))
