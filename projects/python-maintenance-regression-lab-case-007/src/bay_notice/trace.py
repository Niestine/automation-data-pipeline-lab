"""Frozen correlated-failure trace for three retry policies.

While a job's fault flag is set, a retry dooms the next job: every attempt of
that next job fails. The draw list is part of the fixture, so admission is exact.
"""

from __future__ import annotations

from .budget import ReplayRandom, SharedBudget


def run_policy(jobs: list[dict], policy: str, draws: list[float], base_load: float) -> dict:
    if policy not in {"no-retry", "standard", "budgeted"}:
        raise ValueError(f"unknown policy {policy!r}")
    max_attempts = 1 if policy == "no-retry" else 3
    budget = None
    if policy == "budgeted":
        budget = SharedBudget(base_load=base_load, rng=ReplayRandom(list(draws)))
    doom_next = False
    calls = 0
    successes = 0
    for job in jobs:
        doomed = doom_next
        doom_next = False
        ok = False
        for attempt in range(max_attempts):
            calls += 1
            if doomed:
                outcome_ok = False
            elif attempt == 0:
                outcome_ok = bool(job["first_ok"])
            else:
                outcome_ok = (not job["fault"]) and bool(job["transient"])
            if outcome_ok:
                if budget is not None:
                    budget.observe(False)
                    budget.note_status("OK")
                    budget.tick()
                ok = True
                break
            will_retry = attempt + 1 < max_attempts
            if budget is not None:
                budget.observe(True)
                if job["fault"]:
                    budget.note_status("OVERLOADED")
                    budget.tick()
                    will_retry = False
                else:
                    budget.note_status("OK")
                    budget.tick()
                    if will_retry and not budget.try_admit():
                        will_retry = False
            if will_retry and job["fault"]:
                doom_next = True
            if not will_retry:
                break
        if ok:
            successes += 1
    job_count = len(jobs)
    return {
        "policy": policy,
        "successes": successes,
        "calls": calls,
        "jobs": job_count,
        "success_rate": successes / job_count,
        "raf": calls / job_count,
    }


def compare(jobs: list[dict], draws: list[float], base_load: float) -> dict[str, dict]:
    return {
        name: run_policy(jobs, name, draws, base_load)
        for name in ("no-retry", "standard", "budgeted")
    }
