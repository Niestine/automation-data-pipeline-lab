"""Seeded campaigns, first-failure counts, and the PCT miss budget."""

from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path
from typing import Optional, Sequence, TextIO, Union

from schedlab.artifact import Report, dump, report_path
from schedlab.environment import probe, python_version
from schedlab.logsetup import CampaignLogs, summary_line
from schedlab.machine import Machine
from schedlab.oracles import is_subject_failure
from schedlab.policies import run_fair, run_pct, run_random, search_schedules
from schedlab.soundness import assert_sound
from schedlab.subjects import Subject, atomicity_d2, get_subject

SCREEN_SEEDS = tuple(range(30))
POLICIES = ("pct", "preemption", "delay", "random", "dfs", "fair")
SubjectLike = Union[str, Subject]


def per_run_lower_bound(n_max: int, k: int, d: int) -> float:
    """PCT per-run floor ``1 / (n * k ** (d - 1))`` for one depth."""

    if d < 1 or n_max < 1 or k < 1:
        raise ValueError("n_max, k, and d must be >= 1")
    return 1.0 / (n_max * (k ** (d - 1)))


def minimum_runs(n_max: int, k: int, d: int, delta: float = 0.01) -> int:
    """Smallest R with ``(1 - p) ** R <= delta``. Do not shrink it.

    ``p`` is the per-run lower bound for this depth only. A depth-3 campaign
    does not inherit the depth-1 bound.
    """

    if not 0 < delta < 1:
        raise ValueError("delta must be between 0 and 1")
    probability = per_run_lower_bound(n_max, k, d)
    if probability >= 1.0:
        return 1
    miss = 1.0 - probability
    runs = max(1, math.ceil(math.log(delta) / math.log(miss) - 1e-12))
    while miss ** runs > delta:
        runs += 1
    while runs > 1 and miss ** (runs - 1) <= delta:
        runs -= 1
    return runs


def _resolve(subject: SubjectLike, revision: str, pad: int) -> Subject:
    if isinstance(subject, Subject):
        if subject.revision != revision:
            raise ValueError("subject revision does not match the campaign revision")
        if pad:
            raise ValueError("pass pad through get_subject, not a built Subject")
        return subject
    return get_subject(subject, revision, pad=pad)


def report_from(
    machine: Machine,
    *,
    policy: str,
    seed: int,
    d: int,
    change_points: list[int],
    schedules_used: int,
    coverage: str = "",
    terminal: Optional[str] = None,
) -> Report:
    return Report(
        subject=machine.name,
        revision=machine.revision,
        policy=policy,
        seed=seed,
        n_max=machine.n_max,
        k=machine.k,
        d=d,
        change_points=list(change_points),
        schedule=list(machine.schedule),
        preemptions=machine.preemptions,
        delays=machine.delays,
        steps=machine.step,
        terminal=terminal or machine.terminal or "diverged",
        schedules_used=schedules_used,
        gil=probe(),
        python=python_version(),
        coverage=coverage,
        final_cells=dict(machine.cells),
        thread_locals=machine.thread_locals(),
        max_reentrancy=machine.max_reentrancy,
        notes=list(machine.notes),
        pad=machine.pad,
    )


def _publish(
    report: Report,
    artifact_dir: Optional[Path],
    logs: Optional[CampaignLogs],
    *,
    write_log: bool = True,
) -> None:
    if artifact_dir is None:
        return
    path = dump(report_path(artifact_dir, report), report)
    if logs is not None and write_log:
        logs.emit(
            path.with_suffix(".log"),
            report.notes,
            summary_line(
                policy=report.policy,
                seed=report.seed,
                terminal=report.terminal,
                steps=report.steps,
                schedules_used=report.schedules_used,
            ),
        )


def _blank(subject: Subject, n_max: int, k: int) -> Machine:
    return Machine.from_subject(subject, n_max=n_max, k=k)


def run_campaign(
    subject: SubjectLike,
    revision: str,
    policy: str,
    seeds: Sequence[int] = (),
    n_max: int = 3,
    k: Optional[int] = None,
    d: int = 1,
    schedule_cap: int = 500,
    artifact_dir: Optional[Path] = None,
    console_stream: Optional[TextIO] = None,
    max_bound: int = 5,
    pad: int = 0,
) -> Report:
    """Return the first failing artifact, or a cap / exhaustion report.

    ``step-cap`` and ``bound_exceeded`` are not subject failures. ``deadlock``
    and a rejecting predicate are. The schedule cap stops the campaign even
    when later seeds remain.
    """

    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy}")
    if schedule_cap < 1:
        raise ValueError("schedule_cap must be >= 1")
    if max_bound < 0:
        raise ValueError("max_bound must be >= 0")
    built = _resolve(subject, revision, pad)
    assert_sound(built)
    if k is None:
        k = built.step_budget()
    if k < 1 or n_max < 1:
        raise ValueError("n_max and k must be >= 1")
    seed_list = [int(seed) for seed in seeds]
    if any(seed < 0 for seed in seed_list):
        raise ValueError("seeds must be >= 0")
    directory = Path(artifact_dir) if artifact_dir is not None else None
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    logs = CampaignLogs(console_stream) if directory is not None or console_stream is not None else None
    try:
        if policy in ("pct", "random"):
            return _seeded(
                built,
                policy=policy,
                seeds=seed_list,
                n_max=n_max,
                k=k,
                d=d,
                schedule_cap=schedule_cap,
                artifact_dir=directory,
                logs=logs,
            )
        if policy == "fair":
            machine = run_fair(built, n_max=n_max, k=k)
            report = report_from(
                machine,
                policy="fair",
                seed=0,
                d=0,
                change_points=[],
                schedules_used=1,
            )
            _publish(report, directory, logs)
            return report
        return _bounded(
            built,
            policy=policy,
            n_max=n_max,
            k=k,
            schedule_cap=schedule_cap,
            max_bound=max_bound,
            artifact_dir=directory,
            logs=logs,
        )
    finally:
        if logs is not None:
            logs.close()


def _seeded(
    subject: Subject,
    *,
    policy: str,
    seeds: list[int],
    n_max: int,
    k: int,
    d: int,
    schedule_cap: int,
    artifact_dir: Optional[Path],
    logs: Optional[CampaignLogs],
) -> Report:
    if not seeds:
        raise ValueError(f"{policy} requires at least one seed")
    if policy == "pct" and d < 1:
        raise ValueError("PCT depth d must be >= 1")
    used = 0
    last: Optional[Report] = None
    for seed in seeds:
        if used >= schedule_cap:
            break
        if policy == "pct":
            trace = run_pct(subject, seed=seed, n_max=n_max, k=k, d=d)
            machine = trace.machine
            change_points = trace.change_points
            depth = d
        else:
            machine = run_random(subject, seed=seed, n_max=n_max, k=k)
            change_points = []
            depth = 0
        used += 1
        last = report_from(
            machine,
            policy=policy,
            seed=seed,
            d=depth,
            change_points=change_points,
            schedules_used=used,
        )
        _publish(last, artifact_dir, logs)
        if is_subject_failure(last.terminal):
            return last
    assert last is not None
    if used >= schedule_cap and used < len(seeds):
        return _summary(
            subject,
            policy=policy,
            n_max=n_max,
            k=k,
            d=d if policy == "pct" else 0,
            schedules_used=schedule_cap,
            terminal="cap",
            coverage="schedule cap reached",
            artifact_dir=artifact_dir,
            logs=logs,
        )
    last.coverage = last.coverage or "no failure in the supplied seeds"
    _publish(last, artifact_dir, logs, write_log=False)
    return last


def _bounded(
    subject: Subject,
    *,
    policy: str,
    n_max: int,
    k: int,
    schedule_cap: int,
    max_bound: int,
    artifact_dir: Optional[Path],
    logs: Optional[CampaignLogs],
) -> Report:
    total = 0
    coverage = ""
    if policy == "dfs":
        bounds = (0,)
        mode = "dfs"
    else:
        bounds = tuple(range(max_bound + 1))
        mode = policy
    for bound in bounds:
        remaining = schedule_cap - total
        if remaining <= 0:
            return _summary(
                subject,
                policy=policy,
                n_max=n_max,
                k=k,
                d=bound,
                schedules_used=schedule_cap,
                terminal="cap",
                coverage="schedule cap reached",
                artifact_dir=artifact_dir,
                logs=logs,
            )
        outcome = search_schedules(
            subject,
            mode=mode,
            bound=bound,
            n_max=n_max,
            k=k,
            schedule_cap=remaining,
            stop_on_failure=True,
        )
        for offset, machine in enumerate(outcome.completed, start=1):
            total += 1
            report = report_from(
                machine,
                policy=policy,
                seed=offset,
                d=0 if policy == "dfs" else bound,
                change_points=[],
                schedules_used=total,
            )
            _publish(report, artifact_dir, logs)
            if is_subject_failure(report.terminal):
                return report
        if outcome.hit_cap:
            return _summary(
                subject,
                policy=policy,
                n_max=n_max,
                k=k,
                d=0 if policy == "dfs" else bound,
                schedules_used=schedule_cap,
                terminal="cap",
                coverage="schedule cap reached",
                artifact_dir=artifact_dir,
                logs=logs,
            )
        coverage = outcome.coverage
    return _summary(
        subject,
        policy=policy,
        n_max=n_max,
        k=k,
        d=0 if policy == "dfs" else max_bound,
        schedules_used=total,
        terminal="pass",
        coverage=coverage,
        artifact_dir=artifact_dir,
        logs=logs,
    )


def _summary(
    subject: Subject,
    *,
    policy: str,
    n_max: int,
    k: int,
    d: int,
    schedules_used: int,
    terminal: str,
    coverage: str,
    artifact_dir: Optional[Path],
    logs: Optional[CampaignLogs],
) -> Report:
    blank = _blank(subject, n_max, k)
    # The summary is not a program execution. Drop the initial terminal.
    report = report_from(
        blank,
        policy=policy,
        seed=-1,
        d=d,
        change_points=[],
        schedules_used=schedules_used,
        coverage=coverage,
        terminal=terminal,
    )
    report.schedule = []
    report.steps = 0
    report.preemptions = 0
    report.delays = 0
    report.notes = []
    report.final_cells = {}
    report.max_reentrancy = 0
    _publish(report, artifact_dir, logs)
    return report


def failure_count(
    name: str,
    revision: str,
    seeds: Sequence[int],
    *,
    pad: int = 0,
    n_max: int = 3,
) -> int:
    """How many controlled-random seeds reach a subject failure."""

    hits = 0
    for seed in seeds:
        subject = get_subject(name, revision, pad=pad if name == "atomicity_d2" else 0)
        k = subject.step_budget()
        width = max(n_max, len(subject.ops))
        machine = run_random(subject, seed=int(seed), n_max=width, k=k)
        if is_subject_failure(machine.terminal or ""):
            hits += 1
    return hits


def _state_key(machine: Machine) -> tuple[object, ...]:
    threads = tuple(
        (
            tid,
            thread.pc,
            thread.blocked_on,
            thread.finished,
            tuple(sorted((key, repr(value)) for key, value in thread.locals.items())),
        )
        for tid, thread in sorted(machine.threads.items())
    )
    waiters = tuple(sorted((name, tuple(sorted(tids))) for name, tids in machine.waiters.items()))
    return (
        threads,
        tuple(sorted(machine.cells.items())),
        tuple(sorted(machine.locks.items())),
        waiters,
        machine.step,
    )


def exact_failure_rate(subject: Subject, *, n_max: int = 3) -> Fraction:
    """Exact probability that controlled random reaches ``fail`` or ``deadlock``.

    Every enabled tid is weighted ``1 / len(enabled)``, which is what
    ``run_random`` draws. The whole choice tree is walked with memoisation on
    the machine state, so the answer does not depend on which seeds a screen
    happens to use.
    """

    k = subject.step_budget()
    width = max(n_max, len(subject.ops))
    memo: dict[tuple[object, ...], Fraction] = {}

    def walk(machine: Machine) -> Fraction:
        if machine.terminal is not None:
            return Fraction(1 if is_subject_failure(machine.terminal) else 0)
        key = _state_key(machine)
        if key in memo:
            return memo[key]
        enabled = machine.enabled_tids()
        total = Fraction(0)
        for tid in enabled:
            child = machine.clone()
            child.execute(tid)
            total += walk(child)
        rate = total / len(enabled) if enabled else Fraction(0)
        memo[key] = rate
        return rate

    return walk(Machine.from_subject(subject, n_max=width, k=k))


def fit_atomicity_pad(max_k: int = 32) -> int:
    """Smallest pad whose exact random failure rate is under one half.

    Pads are identical dummy reads placed before the snapshot on both twins.
    Stops at the largest pad that keeps ``k`` within ``max_k`` and returns it
    even if the rate is still at least one half; the screen then reports the
    subject as trivial. Extra threads are not used: they mostly enlarge the
    schedule space.
    """

    pad = 0
    while True:
        subject = atomicity_d2("buggy", pad=pad)
        if exact_failure_rate(subject) * 2 < 1:
            return pad
        if subject.step_budget() + len(subject.ops) > max_k:
            return pad
        pad += 1


def trivial_screen(
    seeds: Sequence[int] = SCREEN_SEEDS,
    *,
    pad: Optional[int] = None,
    max_k: int = 32,
) -> dict[str, object]:
    """Tag a subject trivial when controlled random fails it at least half the time.

    The tag uses the exact rate. The fixed seeds are also run and reported as
    ``hits``, so the sampled count stays visible next to the exact value.
    """

    seed_list = [int(seed) for seed in seeds]
    if not seed_list:
        raise ValueError("screen seeds are empty")
    chosen = fit_atomicity_pad(max_k=max_k) if pad is None else pad
    rows: dict[str, dict[str, object]] = {}
    for name in ("ordering_d1", "atomicity_d2", "deadlock_d2"):
        name_pad = chosen if name == "atomicity_d2" else 0
        exact = exact_failure_rate(get_subject(name, "buggy", pad=name_pad))
        hits = failure_count(name, "buggy", seed_list, pad=name_pad)
        rows[name] = {
            "hits": hits,
            "seeds": len(seed_list),
            "rate": hits / len(seed_list),
            "exact_rate": f"{exact.numerator}/{exact.denominator}",
            "trivial": exact * 2 >= 1,
            "pad": name_pad,
        }
    headline = None
    for candidate in ("atomicity_d2", "deadlock_d2"):
        if not rows[candidate]["trivial"]:
            headline = candidate
            break
    return {
        "rows": rows,
        "headline": headline,
        "pad": chosen,
        "seeds": seed_list,
    }
