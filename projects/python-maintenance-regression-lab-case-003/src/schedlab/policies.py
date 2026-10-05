"""Schedulers over the enabled set.

PCT, preemption bounding, delay bounding, controlled random, DFS, and a fair
round-robin all share one interpreter. The random draws live on
``random.Random(seed)``. The global ``random`` module is not seeded.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from schedlab.logsetup import step_line
from schedlab.machine import Machine
from schedlab.oracles import (
    bound_coverage,
    delay_cost,
    is_subject_failure,
    preemption_cost,
)
from schedlab.subjects import Subject

_REASONS = {
    "pct": "priority",
    "preemption": "preemption",
    "delay": "delay",
    "random": "random",
    "dfs": "dfs",
    "fair": "fair",
    "replay": "replay",
}


@dataclass
class PctTrace:
    machine: Machine
    change_points: list[int]
    initial_priorities: dict[int, int]
    priorities_after: list[dict[int, int]]


@dataclass
class SearchOutcome:
    schedules_used: int
    failure: Optional[Machine] = None
    terminals: dict[str, int] = field(default_factory=dict)
    coverage: str = ""
    hit_cap: bool = False
    completed: list[Machine] = field(default_factory=list)


def note_choice(machine: Machine, tid: int, prev_tid: Optional[int], reason: str) -> bool:
    """Record one scheduler decision. Returns false when ``tid`` cannot run."""

    enabled = machine.enabled_tids()
    if tid not in enabled:
        return False
    machine.notes.append(
        step_line(step=machine.step + 1, tid=tid, enabled=enabled, reason=reason)
    )
    machine.preemptions += preemption_cost(enabled, tid, prev_tid)
    machine.delays += delay_cost(enabled, tid)
    return True


def run_pct(subject: Subject, *, seed: int, n_max: int, k: int, d: int) -> PctTrace:
    """Priority schedule with ``d - 1`` change points drawn before the run.

    Lowering happens after the step, so the thread that just ran is the one
    whose key becomes strictly smaller than every other key.
    """

    if d < 1:
        raise ValueError("PCT depth d must be >= 1")
    if d - 1 > k:
        raise ValueError("d - 1 change points do not fit in k steps")
    rng = random.Random(seed)
    if d == 1:
        change_points: set[int] = set()
    else:
        change_points = set(rng.sample(list(range(1, k + 1)), d - 1))
    live: dict[int, int] = {}

    def on_create(tid: int) -> None:
        key = rng.randrange(1 << 62)
        while key in live.values():
            key = rng.randrange(1 << 62)
        live[tid] = key

    machine = Machine.from_subject(subject, n_max=n_max, k=k)
    for tid in sorted(machine.threads):
        on_create(tid)
    initial = dict(live)
    snapshots: list[dict[int, int]] = []
    prev: Optional[int] = None
    while machine.terminal is None:
        enabled = machine.enabled_tids()
        if not enabled:
            machine.terminal = "diverged"
            break
        tid = max(enabled, key=lambda item: live[item])
        if not note_choice(machine, tid, prev, _REASONS["pct"]):
            machine.terminal = "diverged"
            break
        prev = tid
        machine.execute(tid)
        if machine.step in change_points and tid in live:
            live[tid] = min(live.values()) - 1
        snapshots.append(dict(live))
    return PctTrace(
        machine=machine,
        change_points=sorted(change_points),
        initial_priorities=initial,
        priorities_after=snapshots,
    )


def run_random(subject: Subject, *, seed: int, n_max: int, k: int) -> Machine:
    """Uniform choice among enabled tids. The seed makes the run replayable."""

    rng = random.Random(seed)
    machine = Machine.from_subject(subject, n_max=n_max, k=k)
    prev: Optional[int] = None
    while machine.terminal is None:
        enabled = machine.enabled_tids()
        if not enabled:
            machine.terminal = "diverged"
            break
        tid = rng.choice(enabled)
        if not note_choice(machine, tid, prev, _REASONS["random"]):
            machine.terminal = "diverged"
            break
        prev = tid
        machine.execute(tid)
    return machine


def run_fair(subject: Subject, *, n_max: int, k: int) -> Machine:
    """Oldest ``last_step``, then lowest tid. Used for liveness, not for PCT."""

    machine = Machine.from_subject(subject, n_max=n_max, k=k)
    prev: Optional[int] = None
    while machine.terminal is None:
        enabled = machine.enabled_threads()
        if not enabled:
            machine.terminal = "diverged"
            break
        enabled.sort(key=lambda thread: (thread.last_step, thread.tid))
        tid = enabled[0].tid
        if not note_choice(machine, tid, prev, _REASONS["fair"]):
            machine.terminal = "diverged"
            break
        prev = tid
        machine.execute(tid)
    return machine


def search_schedules(
    subject: Subject,
    *,
    mode: str,
    bound: int,
    n_max: int,
    k: int,
    schedule_cap: int,
    stop_on_failure: bool = True,
) -> SearchOutcome:
    """Depth-first search of schedules whose preemption or delay cost is <= bound.

    ``mode="dfs"`` ignores the bound and tries enabled tids in ascending order.
    A finished search sets ``coverage`` to the bound claim. It does not say the
    subject is free of bugs above that bound.
    """

    if mode not in ("preemption", "delay", "dfs"):
        raise ValueError(f"unknown search mode {mode}")
    if schedule_cap < 1:
        raise ValueError("schedule_cap must be >= 1")
    if mode != "dfs" and bound < 0:
        raise ValueError("bound must be >= 0")
    outcome = SearchOutcome(schedules_used=0)
    aborted = False

    def finish(machine: Machine) -> None:
        nonlocal aborted
        outcome.schedules_used += 1
        terminal = machine.terminal or "diverged"
        outcome.terminals[terminal] = outcome.terminals.get(terminal, 0) + 1
        outcome.completed.append(machine)
        if stop_on_failure and is_subject_failure(terminal):
            outcome.failure = machine

    def dfs(machine: Machine, prev: Optional[int], budget: int) -> None:
        nonlocal aborted
        if outcome.failure is not None or aborted or outcome.schedules_used >= schedule_cap:
            aborted = outcome.schedules_used >= schedule_cap and outcome.failure is None
            return
        if machine.terminal is not None:
            finish(machine)
            return
        enabled = machine.enabled_tids()
        if not enabled:
            machine.terminal = "diverged"
            finish(machine)
            return
        legal: list[tuple[int, int, int]] = []
        for tid in enabled:
            p_cost = preemption_cost(enabled, tid, prev)
            d_cost = delay_cost(enabled, tid)
            if mode == "preemption" and p_cost > budget:
                continue
            if mode == "delay" and d_cost > budget:
                continue
            legal.append((tid, p_cost, d_cost))
        if not legal:
            machine.terminal = "diverged"
            finish(machine)
            return
        reason = _REASONS["dfs" if mode == "dfs" else mode]
        for index, (tid, p_cost, d_cost) in enumerate(legal):
            child = machine.clone()
            child.notes.append(
                step_line(step=machine.step + 1, tid=tid, enabled=enabled, reason=reason)
            )
            child.preemptions += p_cost
            child.delays += d_cost
            child.execute(tid)
            if mode == "preemption":
                next_budget = budget - p_cost
            elif mode == "delay":
                next_budget = budget - d_cost
            else:
                next_budget = budget
            dfs(child, tid, next_budget)
            if outcome.failure is not None:
                return
            if outcome.schedules_used >= schedule_cap and index + 1 < len(legal):
                aborted = True
                return
            if aborted:
                return

    root = Machine.from_subject(subject, n_max=n_max, k=k)
    start_budget = 0 if mode == "dfs" else bound
    dfs(root, None, start_budget)
    outcome.hit_cap = aborted and outcome.failure is None
    if outcome.failure is None and not outcome.hit_cap:
        if mode == "dfs":
            outcome.coverage = "dfs exhausted"
        else:
            outcome.coverage = bound_coverage(bound)
    return outcome
