"""Censored per-URL change rate.

Several edits inside one sample interval count as one detection. A URL that
changes on every comparable sample is rate-censored and cannot look slower
than one change per sample interval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RateEstimate:
    lam: float
    censored: bool
    count: int
    span: float
    comparable: int


def estimate_rate(
    material_count: int,
    span: float,
    comparable: int,
    sample_interval: float,
) -> RateEstimate:
    if comparable < 0:
        raise ValueError("comparable must be >= 0")
    if sample_interval <= 0:
        raise ValueError("sample_interval must be > 0")
    if material_count <= 0 or span <= 0 or comparable <= 0:
        return RateEstimate(0.0, False, max(0, material_count), max(0.0, span), comparable)
    lam = material_count / span
    censored = material_count >= comparable
    if censored:
        lam = max(lam, 1.0 / sample_interval)
    return RateEstimate(lam, censored, material_count, span, comparable)


def expected_freshness(lam: float, t: float) -> float:
    """E[freshness] = exp(-λ t) at time t after a sync."""
    if t < 0:
        raise ValueError("t must be >= 0")
    if lam < 0:
        raise ValueError("lam must be >= 0")
    if lam == 0 or t == 0:
        return 1.0
    return math.exp(-lam * t)


def expected_age(lam: float, t: float) -> float:
    """E[age] = t * (1 - (1 - exp(-λ t)) / (λ t)) for λ > 0, else 0."""
    if t < 0:
        raise ValueError("t must be >= 0")
    if lam < 0:
        raise ValueError("lam must be >= 0")
    if lam == 0 or t == 0:
        return 0.0
    # Same value as t * (1 - (1 - e^(-λt)) / (λt)), written with expm1 for small λt.
    return t + math.expm1(-lam * t) / lam


def time_average_freshness(lam: float, interval: float) -> float:
    """(1 - exp(-λ I)) / (λ I) over a refresh interval that starts at a sync."""
    if interval <= 0:
        raise ValueError("interval must be > 0")
    if lam < 0:
        raise ValueError("lam must be >= 0")
    if lam == 0:
        return 1.0
    return -math.expm1(-lam * interval) / (lam * interval)
