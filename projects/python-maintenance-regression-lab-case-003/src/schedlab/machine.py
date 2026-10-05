"""One-op-at-a-time interpreter for logical threads.

A step runs exactly one instrumented operation of one enabled thread.
Shared state is integer cells and locks. Plain Python containers are not
scheduling points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from schedlab.errors import MachineError

SKIP = "skip"

Predicate = Callable[["Machine"], bool]


@dataclass(frozen=True)
class Op:
    """A single scheduling point."""

    kind: str
    name: str = ""
    value: int = 0
    local: str = ""
    flag: str = ""
    flag_value: int = 0
    sentinel: str = ""


@dataclass
class ThreadState:
    tid: int
    ops: tuple[Op, ...]
    pc: int = 0
    blocked_on: Optional[str] = None
    finished: bool = False
    last_step: int = 0
    locals: dict[str, object] = field(default_factory=dict)

    def clone(self) -> "ThreadState":
        return ThreadState(
            tid=self.tid,
            ops=self.ops,
            pc=self.pc,
            blocked_on=self.blocked_on,
            finished=self.finished,
            last_step=self.last_step,
            locals=dict(self.locals),
        )


class Machine:
    """Logical threads, locks, and integer cells."""

    def __init__(
        self,
        *,
        name: str,
        revision: str,
        ops: dict[int, tuple[Op, ...]],
        cells: dict[str, int],
        locks: tuple[str, ...],
        predicate: Predicate,
        n_max: int,
        k: int,
        pad: int = 0,
    ) -> None:
        if n_max < 1 or k < 1:
            raise MachineError("n_max and k must be >= 1")
        self.name = name
        self.revision = revision
        self.pad = pad
        self.n_max = n_max
        self.k = k
        self.predicate = predicate
        self.cells = dict(cells)
        self.locks: dict[str, Optional[int]] = {lock: None for lock in locks}
        self.waiters: dict[str, set[int]] = {lock: set() for lock in locks}
        self.threads: dict[int, ThreadState] = {}
        for tid in sorted(ops):
            thread = ThreadState(tid=tid, ops=tuple(ops[tid]))
            if thread.pc >= len(thread.ops):
                thread.finished = True
            self.threads[tid] = thread
        self.step = 0
        self.schedule: list[int] = []
        self.preemptions = 0
        self.delays = 0
        self.notes: list[str] = []
        self.terminal: Optional[str] = None
        self.max_reentrancy = 0
        self._depth = 0
        if len(self.threads) > n_max:
            self.terminal = "bound_exceeded"
        else:
            self._classify(allow_step_cap=False)

    @classmethod
    def from_subject(cls, subject: object, n_max: int, k: int) -> "Machine":
        return cls(
            name=subject.name,  # type: ignore[attr-defined]
            revision=subject.revision,  # type: ignore[attr-defined]
            ops=subject.ops,  # type: ignore[attr-defined]
            cells=subject.cells,  # type: ignore[attr-defined]
            locks=subject.locks,  # type: ignore[attr-defined]
            predicate=subject.predicate,  # type: ignore[attr-defined]
            n_max=n_max,
            k=k,
            pad=getattr(subject, "pad", 0),
        )

    def clone(self) -> "Machine":
        other = Machine.__new__(Machine)
        other.name = self.name
        other.revision = self.revision
        other.pad = self.pad
        other.n_max = self.n_max
        other.k = self.k
        other.predicate = self.predicate
        other.cells = dict(self.cells)
        other.locks = dict(self.locks)
        other.waiters = {name: set(tids) for name, tids in self.waiters.items()}
        other.threads = {tid: thread.clone() for tid, thread in self.threads.items()}
        other.step = self.step
        other.schedule = list(self.schedule)
        other.preemptions = self.preemptions
        other.delays = self.delays
        other.notes = list(self.notes)
        other.terminal = self.terminal
        other.max_reentrancy = self.max_reentrancy
        other._depth = 0
        return other

    def enabled_threads(self) -> list[ThreadState]:
        return [
            thread
            for thread in self.threads.values()
            if not thread.finished and thread.blocked_on is None
        ]

    def enabled_tids(self) -> list[int]:
        return sorted(thread.tid for thread in self.enabled_threads())

    def blocked_tids(self) -> list[int]:
        return sorted(
            thread.tid
            for thread in self.threads.values()
            if thread.blocked_on is not None and not thread.finished
        )

    def execute(self, tid: int) -> None:
        """Run one op of ``tid``. The interpreter does not re-enter."""

        if self._depth != 0:
            raise MachineError("interpreter re-entered")
        self._depth += 1
        self.max_reentrancy = max(self.max_reentrancy, self._depth)
        try:
            if self.terminal is not None:
                raise MachineError(f"machine already stopped: {self.terminal}")
            if self.step >= self.k:
                self.terminal = "bound_exceeded"
                return
            enabled = self.enabled_tids()
            if tid not in enabled:
                raise MachineError(f"thread {tid} is not enabled")
            thread = self.threads[tid]
            op = thread.ops[thread.pc]
            self._known(op)
            self.step += 1
            thread.last_step = self.step
            self.schedule.append(tid)
            self._apply(thread, op)
            self._classify(allow_step_cap=True)
        finally:
            self._depth -= 1

    def thread_locals(self) -> dict[str, dict[str, object]]:
        return {str(tid): dict(thread.locals) for tid, thread in self.threads.items()}

    def _known(self, op: Op) -> None:
        known = {
            "read",
            "write",
            "write_add",
            "rmw_add",
            "acquire",
            "release",
            "read_if",
        }
        if op.kind not in known:
            raise MachineError(f"unknown op {op.kind}")

    def _apply(self, thread: ThreadState, op: Op) -> None:
        if op.kind == "read":
            thread.locals[op.local] = self._cell(op.name)
            self._advance(thread)
        elif op.kind == "write":
            self.cells[op.name] = op.value
            self._advance(thread)
        elif op.kind == "write_add":
            if op.local not in thread.locals:
                raise MachineError(f"thread {thread.tid} has no local {op.local}")
            snapshot = thread.locals[op.local]
            if not isinstance(snapshot, int) or isinstance(snapshot, bool):
                raise MachineError("write_add snapshot is not an int")
            self.cells[op.name] = snapshot + 1
            self._advance(thread)
        elif op.kind == "rmw_add":
            self.cells[op.name] = self._cell(op.name) + 1
            self._advance(thread)
        elif op.kind == "read_if":
            seen = thread.locals.get(op.flag)
            if seen == op.flag_value:
                thread.locals[op.local] = self._cell(op.name)
            else:
                thread.locals[op.local] = op.sentinel
            self._advance(thread)
        elif op.kind == "acquire":
            self._acquire(thread, op.name)
        elif op.kind == "release":
            self._release(thread, op.name)

    def _cell(self, name: str) -> int:
        if name not in self.cells:
            raise MachineError(f"missing cell {name}")
        value = self.cells[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise MachineError(f"cell {name} is not an int")
        return value

    def _advance(self, thread: ThreadState) -> None:
        thread.pc += 1
        if thread.pc >= len(thread.ops) and thread.blocked_on is None:
            thread.finished = True

    def _acquire(self, thread: ThreadState, name: str) -> None:
        if name not in self.locks:
            raise MachineError(f"missing lock {name}")
        holder = self.locks[name]
        if holder is None:
            self.locks[name] = thread.tid
            self._advance(thread)
            return
        thread.blocked_on = name
        self.waiters[name].add(thread.tid)
        self._advance(thread)

    def _release(self, thread: ThreadState, name: str) -> None:
        if name not in self.locks:
            raise MachineError(f"missing lock {name}")
        if self.locks[name] != thread.tid:
            raise MachineError(f"thread {thread.tid} does not hold {name}")
        waiters = self.waiters[name]
        if waiters:
            wake_tid = min(waiters)
            waiters.remove(wake_tid)
            woken = self.threads[wake_tid]
            woken.blocked_on = None
            self.locks[name] = wake_tid
            if woken.pc >= len(woken.ops):
                woken.finished = True
        else:
            self.locks[name] = None
        self._advance(thread)

    def _classify(self, *, allow_step_cap: bool) -> None:
        if self.terminal is not None:
            return
        enabled = self.enabled_threads()
        blocked = self.blocked_tids()
        if not enabled and blocked:
            self.terminal = "deadlock"
            return
        if self.threads and all(thread.finished for thread in self.threads.values()):
            self.terminal = "pass" if self._accepts() else "fail"
            return
        if allow_step_cap and self.step >= self.k and enabled:
            self.terminal = "step-cap"

    def _accepts(self) -> bool:
        return bool(self.predicate(self))
