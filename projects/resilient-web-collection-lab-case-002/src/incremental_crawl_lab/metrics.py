"""Freshness and age from a fixture change log.

Freshness is 1 only while the stored copy matches the origin. Age is 0 while
it matches, otherwise now minus the first origin modification after the sync.
Later edits before the next sync do not move either metric.
"""

from __future__ import annotations


def _last_sync(sync_times: list[float], t: float) -> float | None:
    prior = [item for item in sync_times if item <= t]
    if not prior:
        return None
    return max(prior)


def _changes_after(change_times: list[float], sync: float, t: float) -> list[float]:
    return sorted(change for change in change_times if sync < change <= t)


def freshness_at(sync_times: list[float], change_times: list[float], t: float) -> int:
    sync = _last_sync(sync_times, t)
    if sync is None:
        return 0
    return 0 if _changes_after(change_times, sync, t) else 1


def age_at(sync_times: list[float], change_times: list[float], t: float) -> float:
    sync = _last_sync(sync_times, t)
    if sync is None:
        return 0.0
    later = _changes_after(change_times, sync, t)
    if not later:
        return 0.0
    return t - later[0]


def time_average(
    sync_times: list[float],
    change_times: list[float],
    t0: float,
    t1: float,
) -> tuple[float, float]:
    """Return (time-average freshness, time-average age) on [t0, t1].

    The interval before the first sync is outside the element's residency and
    is excluded. A sync at t0 covers the whole window.
    """
    if t1 <= t0:
        raise ValueError("window must be non-empty")
    if not sync_times:
        return 0.0, 0.0
    start = max(t0, min(sync_times))
    if start >= t1:
        return 0.0, 0.0
    points = {start, t1}
    for sync in sync_times:
        if start < sync < t1:
            points.add(sync)
    for change in change_times:
        if start < change < t1:
            points.add(change)
    ordered = sorted(points)
    fresh_time = 0.0
    age_integral = 0.0
    for left, right in zip(ordered, ordered[1:]):
        # State is taken at `left`. The sample at `right` belongs to the next piece,
        # so a sync on the boundary does not leak backward into this integral.
        width = right - left
        if freshness_at(sync_times, change_times, left):
            fresh_time += width
        else:
            age_left = age_at(sync_times, change_times, left)
            age_integral += width * age_left + (width * width) / 2.0
    duration = t1 - start
    return fresh_time / duration, age_integral / duration


def collection_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
