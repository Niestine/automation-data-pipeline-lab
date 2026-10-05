"""Planted buggy and fixed twins.

The two data-race twins keep the same yield points, so a recorded thread
sequence stays feasible after the fix. The deadlock twin changes lock order,
so the old sequence may come back ``diverged``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from schedlab.machine import SKIP, Op, Predicate

INIT_TID = 1
USE_TID = 2


@dataclass(frozen=True)
class Subject:
    name: str
    revision: str
    ops: dict[int, tuple[Op, ...]]
    cells: dict[str, int]
    locks: tuple[str, ...]
    predicate: Predicate
    audit_fns: tuple[Callable[..., object], ...] = ()
    pad: int = 0

    def step_budget(self) -> int:
        return sum(len(thread_ops) for thread_ops in self.ops.values())


def _require_revision(revision: str) -> None:
    if revision not in ("buggy", "fixed"):
        raise ValueError(f"revision must be buggy or fixed, got {revision}")


def _ordering_ok(machine: object) -> bool:
    threads = machine.threads  # type: ignore[attr-defined]
    observed = threads[USE_TID].locals.get("observed")
    return observed == 1 or observed == SKIP


def ordering_d1(revision: str) -> Subject:
    """Depth-1 ordering bug: a payload read before its ready flag.

    Init writes ``value`` then ``ready``. The buggy user reads ``value``
    first. The fixed user reads ``ready`` and skips the payload when the
    flag is clear. Failing shape: user, user, init, init.
    """

    _require_revision(revision)
    init = (
        Op("write", name="value", value=1),
        Op("write", name="ready", value=1),
    )
    if revision == "buggy":
        use = (
            Op("read", name="value", local="observed"),
            Op("read", name="ready", local="unused"),
        )
    else:
        use = (
            Op("read", name="ready", local="seen"),
            Op(
                "read_if",
                name="value",
                local="observed",
                flag="seen",
                flag_value=1,
                sentinel=SKIP,
            ),
        )
    return Subject(
        name="ordering_d1",
        revision=revision,
        ops={INIT_TID: init, USE_TID: use},
        cells={"value": 0, "ready": 0},
        locks=(),
        predicate=_ordering_ok,
    )


def _atomic_ok(machine: object) -> bool:
    return machine.cells.get("x") == 2  # type: ignore[attr-defined]


def atomicity_d2(revision: str, pad: int = 0) -> Subject:
    """Depth-2 check-then-act: both threads snapshot ``x`` and store snapshot+1.

    The fixed twin increments the live cell inside the second op. ``pad``
    inserts the same dummy reads *before* the snapshot on both twins so the
    bug gets rarer under controlled random without adding threads. Reads
    placed between the snapshot and the store would widen the race window
    and make the bug more likely, not less.
    """

    _require_revision(revision)
    if pad < 0:
        raise ValueError("pad must be >= 0")
    second = (
        Op("write_add", name="x", local="snap")
        if revision == "buggy"
        else Op("rmw_add", name="x")
    )
    padding = tuple(Op("read", name="pad", local=f"pad{index}") for index in range(pad))
    body = padding + (Op("read", name="x", local="snap"), second)
    cells = {"x": 0}
    if pad:
        cells["pad"] = 0
    return Subject(
        name="atomicity_d2",
        revision=revision,
        ops={1: body, 2: body},
        cells=cells,
        locks=(),
        predicate=_atomic_ok,
        pad=pad,
    )


def _finished_ok(_machine: object) -> bool:
    return True


def deadlock_d2(revision: str) -> Subject:
    """Depth-2 lock-order deadlock.

    Buggy: thread 1 takes ``a`` then ``b``, thread 2 takes ``b`` then ``a``.
    Fixed: both take ``a`` then ``b``.
    """

    _require_revision(revision)
    ordered = (
        Op("acquire", name="a"),
        Op("acquire", name="b"),
        Op("release", name="b"),
        Op("release", name="a"),
    )
    opposite = (
        Op("acquire", name="b"),
        Op("acquire", name="a"),
        Op("release", name="a"),
        Op("release", name="b"),
    )
    second = opposite if revision == "buggy" else ordered
    return Subject(
        name="deadlock_d2",
        revision=revision,
        ops={1: ordered, 2: second},
        cells={},
        locks=("a", "b"),
        predicate=_finished_ok,
    )


def spin_subject(threads: int, ops_each: int, *, name: str = "spin") -> Subject:
    """Lock-free subject that stays enabled until its reads are consumed."""

    if threads < 1 or ops_each < 1:
        raise ValueError("spin subject needs at least one op on one thread")
    ops = {
        tid: tuple(Op("read", name="c", local="v") for _ in range(ops_each))
        for tid in range(1, threads + 1)
    }
    return Subject(
        name=name,
        revision="fixed",
        ops=ops,
        cells={"c": 0},
        locks=(),
        predicate=_finished_ok,
    )


_BUILDERS: dict[str, Callable[..., Subject]] = {
    "ordering_d1": ordering_d1,
    "atomicity_d2": atomicity_d2,
    "deadlock_d2": deadlock_d2,
}

SUBJECT_NAMES = tuple(_BUILDERS)


def get_subject(name: str, revision: str, *, pad: int = 0) -> Subject:
    if name not in _BUILDERS:
        raise ValueError(f"unknown subject {name}")
    if name == "atomicity_d2":
        return atomicity_d2(revision, pad=pad)
    if pad:
        raise ValueError(f"{name} does not take pad reads")
    return _BUILDERS[name](revision)
