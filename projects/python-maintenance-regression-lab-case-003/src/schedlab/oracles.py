"""Terminal classes and the costs a scheduler charges for a choice."""

from __future__ import annotations

FAILURE_TERMINALS = frozenset({"fail", "deadlock"})
INCONCLUSIVE_TERMINALS = frozenset({"step-cap", "bound_exceeded", "cap", "diverged"})


def is_subject_failure(terminal: str) -> bool:
    """True only for a rejecting predicate or a deadlock.

    ``step-cap``, ``bound_exceeded``, ``cap``, and ``diverged`` are harness
    results. They do not count as the subject failing.
    """

    return terminal in FAILURE_TERMINALS


def preemption_cost(enabled: list[int], tid: int, prev_tid: int | None) -> int:
    """A preemption is a switch away from a thread that is still enabled.

    The first dispatch has no previous thread. A switch after the previous
    thread blocked or finished costs nothing.
    """

    if prev_tid is None:
        return 0
    if prev_tid not in enabled:
        return 0
    if tid != prev_tid:
        return 1
    return 0


def delay_cost(enabled: list[int], tid: int) -> int:
    """One delay when the choice is not the lowest enabled tid.

    That lowest tid is this lab's deterministic baseline. Budget 0 is exactly
    the baseline schedule.
    """

    if not enabled:
        return 0
    if tid == min(enabled):
        return 0
    return 1


def bound_coverage(bound: int) -> str:
    """Claim allowed after a finished search of schedules with at most ``bound``."""

    return f"no failure within bound {bound}"


def data_race_replay_ok(buggy_terminal: str, fixed_terminal: str) -> bool:
    """A data-race fix must replay the same tids to pass. Diverged fails the fixture."""

    return buggy_terminal == "fail" and fixed_terminal == "pass"


def deadlock_replay_ok(
    buggy_terminal: str,
    fixed_terminal: str,
    search_clean: bool,
) -> bool:
    """Buggy replay must deadlock. Fixed replay must not.

    ``diverged`` on the fixed twin is accepted only together with a clean
    bounded search. A replay that still deadlocks means the fix failed.
    """

    if buggy_terminal != "deadlock":
        return False
    if fixed_terminal == "deadlock":
        return False
    if fixed_terminal == "diverged" and search_clean:
        return True
    if fixed_terminal == "pass" and search_clean:
        return True
    return False
