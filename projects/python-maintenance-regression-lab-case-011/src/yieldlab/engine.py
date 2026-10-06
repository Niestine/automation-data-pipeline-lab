"""One-thread-at-a-time yield simulator.

Steps are explicit ops. GIL mode keeps the owner on a quantum of
``switch_interval`` steps unless the op detaches. Free mode yields after
every step. PCT priorities follow the ASPLOS 2010 scheduler: higher numbers
win, and a change point rewrites the priority of the thread that just ran.
Happens-before for user cells joins a lock vector only from release into a
later acquire.
"""

from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass, field

from yieldlab.errors import (
    AtomicityViolation,
    BoundExceeded,
    Deadlock,
    Invariant,
    LabError,
    NeverRetrieved,
    Race,
    ReplayDivergence,
    UseAfterFree,
    WrongThread,
)
from yieldlab.journal import make_journal

log = logging.getLogger("yieldlab")

_YIELD_OPS = frozenset({"allow", "await", "stw", "fork_os"})


@dataclass
class Schedule:
    mode: str = "free"
    seed: int = 0
    depth: int = 1
    n_max: int = 8
    k_budget: int = 64
    preemption_bound: int = 0
    switch_interval: int = 1
    policy: str = "pct"
    priorities: dict | None = None
    change_points: list | None = None
    replay_events: list | None = None
    guide: list | None = None
    raise_races: bool = False
    raise_atomicity: bool = False
    strict_tasks: bool = False


@dataclass
class Scenario:
    name: str
    variant: str
    threads: dict
    invariant_id: str = ""
    holds: object | None = None
    unborn: frozenset = field(default_factory=frozenset)
    kinds: dict = field(default_factory=dict)
    cells: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    fuse_flag: bool = False
    finalize_in_stw: bool = False
    debug: bool = False
    slow_callback_steps: int = 100
    loop_tid: int = 1
    respect_handler_level: bool = True
    handler_level: int = 0
    finalizing: bool = False
    release_for_eq: bool = True
    finalizer_tid: int | None = None


@dataclass
class Trace:
    events: list
    sha256: str
    races: list
    violations: list
    logs: list
    cells: dict
    marks: list
    epoch_ops: int
    vector_ops: int
    mode: str
    extra: dict
    journal: dict


class World:
    def __init__(self, scenario: Scenario, schedule: Schedule) -> None:
        self.scenario = scenario
        self.schedule = schedule
        self.mode = schedule.mode
        self.switch_interval = schedule.switch_interval
        self.cells = dict(scenario.cells)
        self.extra = copy.deepcopy(scenario.extra)
        self.threads: dict[int, dict] = {}
        self.locks: dict[str, dict] = {}
        self.acc: dict[int, int] = {}
        self.clocks: dict[int, dict] = {}
        self.locations: dict[str, dict] = {}
        self.lock_vc: dict[str, dict] = {}
        self.locksets: dict[str, set] = {}
        self.events: list = []
        self.races: list = []
        self.violations: list = []
        self.logs: list = list(self.extra.get("logs", []))
        self.marks: list = []
        self.epoch_ops = 0
        self.vector_ops = 0
        self.step = 0
        self.gil_owner: int | None = None
        self.quantum = 0
        self.yielded = True
        self.loop_runner: int | None = None
        self.regions: dict[int, list] = {}
        self.frozen_locs: set[str] = set()
        self.change_points: list = []
        self.priorities: dict[int, int] = {}
        self.priorities_initial: dict[int, int] = {}
        self.demoted = False
        self.debug = scenario.debug
        self.replay_i = 0
        self.guide_i = 0
        self._setup_threads()
        self._setup_schedule()

    def _setup_threads(self) -> None:
        n = len(self.scenario.threads)
        if n > self.schedule.n_max:
            raise BoundExceeded(
                f"scenario creates {n} threads, n_max is {self.schedule.n_max}"
            )
        for tid, ops in sorted(self.scenario.threads.items()):
            status = "unborn" if tid in self.scenario.unborn else "runnable"
            self.threads[tid] = {
                "ops": [tuple(op) for op in ops],
                "pc": 0,
                "status": status,
                "kind": self.scenario.kinds.get(tid, "thread"),
                "attached": True,
                "cb_run": 0,
                "wait_for": None,
                "held": set(),
            }
            self.acc[tid] = 0
            self.clocks[tid] = {tid: 0}
            self.regions[tid] = []
        self.extra.setdefault("dicts", {})
        self.extra.setdefault("lists", {})
        self.extra.setdefault("sets", {})
        self.extra.setdefault("bufs", {})
        self.extra.setdefault("views", {})
        self.extra.setdefault("sorting", {})
        self.extra.setdefault("hidden", {})
        self.extra.setdefault("headers", {})
        self.extra.setdefault("queue", [])
        self.extra.setdefault("delivered", [])
        self.extra.setdefault("skipped", [])
        self.extra.setdefault("logs", self.logs)
        self.extra.setdefault("marks", self.marks)
        self.extra.setdefault("flags", {})
        self.extra.setdefault("cbs", [])
        self.extra.setdefault("ran", [])
        self.extra.setdefault("future", None)
        self.extra.setdefault("task_exc", None)
        self.extra.setdefault("task_retrieved", False)
        self.extra.setdefault("creation", "")
        for name, kind in self.extra.get("lock_kinds", {}).items():
            self._ensure_lock(name, kind)

    def _setup_schedule(self) -> None:
        n = max(self.threads) if self.threads else 1
        n = max(n, self.schedule.n_max)
        if self.schedule.priorities is not None:
            self.priorities = {int(k): int(v) for k, v in self.schedule.priorities.items()}
        else:
            rng = random.Random(self.schedule.seed)
            ranks = list(range(1, n + 1))
            rng.shuffle(ranks)
            depth = self.schedule.depth
            self.priorities = {
                tid: depth + rank - 1 for tid, rank in zip(range(1, n + 1), ranks)
            }
            if self.schedule.change_points is None and depth > 1:
                k = self.schedule.k_budget
                if k < depth - 1:
                    raise BoundExceeded("k_budget is smaller than the change-point sample")
                steps = rng.sample(range(1, k + 1), depth - 1)
                self.change_points = [(step, depth - i) for i, step in enumerate(steps, start=1)]
        if self.schedule.change_points is not None:
            self.change_points = [
                (int(p["step"]), int(p["priority"]))
                if isinstance(p, dict)
                else (int(p[0]), int(p[1]))
                for p in self.schedule.change_points
            ]
        self.priorities_initial = dict(self.priorities)

    def clone(self) -> "World":
        twin = object.__new__(World)
        for key, value in self.__dict__.items():
            if key == "scenario":
                setattr(twin, key, value)
            else:
                setattr(twin, key, copy.deepcopy(value))
        return twin

    def enabled(self) -> list[int]:
        ids = []
        for tid, thread in self.threads.items():
            if thread["status"] != "runnable":
                continue
            if thread["pc"] >= len(thread["ops"]):
                continue
            if thread["kind"] == "loop" and self.loop_runner not in (None, tid):
                continue
            ids.append(tid)
        if not ids:
            return []
        if (
            self.mode == "gil"
            and self.gil_owner in ids
            and not self.yielded
        ):
            return [self.gil_owner]
        return ids

    def pick_highest(self, enabled: list[int]) -> int:
        return max(enabled, key=lambda tid: (self.priorities.get(tid, 0), -tid))

    def natural(self, current: int | None) -> int | None:
        enabled = self.enabled()
        if not enabled:
            return None
        if current in enabled and not self.yielded:
            return current
        if current in enabled and self.yielded:
            if self.demoted:
                best = self.pick_highest(enabled)
                self.demoted = False
                return best
            return current
        self.demoted = False
        # Preemption search has no current thread on the first step. The
        # baseline is the lowest tid so a straight-line run is unique.
        if self.schedule.policy == "search" and current is None:
            return min(enabled)
        return self.pick_highest(enabled)

    def deadlocked(self) -> bool:
        if self.enabled():
            return False
        if self._pending_io():
            return False
        return any(
            thread["status"] in ("blocked_lock", "blocked_join", "blocked_queue", "barrier")
            for thread in self.threads.values()
        )

    def _pending_io(self) -> bool:
        return any(thread["status"] == "blocked_io" for thread in self.threads.values())

    def _wake_io(self, force: bool = False) -> None:
        for thread in self.threads.values():
            if thread["status"] != "blocked_io":
                continue
            if force or thread.get("io_seen"):
                thread["status"] = "runnable"
                thread.pop("io_seen", None)
            else:
                thread["io_seen"] = True

    def _ensure_lock(self, name: str, kind: str = "user") -> dict:
        lock = self.locks.get(name)
        if lock is None:
            lock = {"owner": None, "waiters": [], "kind": kind}
            self.locks[name] = lock
        return lock

    def _held_names(self, tid: int) -> set:
        return set(self.threads[tid]["held"])

    def _user_lock_held(self, tid: int) -> bool:
        return bool(self.threads[tid]["held"])

    def journal_dict(self, failure: str | None) -> dict:
        return make_journal(
            scenario=self.scenario.name,
            variant=self.scenario.variant,
            mode=self.schedule.mode,
            seed=self.schedule.seed,
            depth=self.schedule.depth,
            preemption_bound=self.schedule.preemption_bound,
            switch_interval=self.schedule.switch_interval,
            k_budget=self.schedule.k_budget,
            n_max=self.schedule.n_max,
            priorities=self.priorities_initial,
            change_points=self.change_points,
            events=self.events,
            failure=failure,
            invariant_id=self.scenario.invariant_id,
        )

    def trace(self, failure: str | None = None) -> Trace:
        self.extra["lockset_warnings"] = self.lockset_warnings()
        self.extra["acc"] = dict(self.acc)
        journal = self.journal_dict(failure)
        return Trace(
            events=self.events,
            sha256=journal["trace_sha256"],
            races=list(self.races),
            violations=list(self.violations),
            logs=list(self.logs),
            cells=dict(self.cells),
            marks=list(self.marks),
            epoch_ops=self.epoch_ops,
            vector_ops=self.vector_ops,
            mode=self.mode,
            extra=self.extra,
            journal=journal,
        )

    def fail(self, exc_type: type[LabError], message: str) -> None:
        journal = self.journal_dict(exc_type.__name__)
        log.info(
            "scenario=%s variant=%s failure=%s trace=%s",
            self.scenario.name,
            self.scenario.variant,
            exc_type.__name__,
            journal["trace_sha256"],
        )
        raise exc_type(message, journal)

    def step_thread(self, tid: int) -> None:
        if self.step >= self.schedule.k_budget:
            raise BoundExceeded(
                f"yield {self.step + 1} exceeds k_budget {self.schedule.k_budget}"
            )
        thread = self.threads[tid]
        if thread["status"] != "runnable" or thread["pc"] >= len(thread["ops"]):
            self.fail(ReplayDivergence, f"thread {tid} is not enabled")
        op = thread["ops"][thread["pc"]]
        completed = self._dispatch(tid, op)
        if not completed:
            return
        thread["pc"] += 1
        self._record(tid, op)
        self.step += 1
        self._change_priority(tid)
        self._finish_quantum(tid, op[0])
        self._retire_if_done(tid)
        self._scan_tasks()

    def _record(self, tid: int, op: tuple) -> None:
        args = []
        for item in op[1:]:
            if isinstance(item, (str, int, float, bool)) or item is None:
                args.append(item)
            else:
                args.append(repr(item))
        self.events.append(
            {"i": len(self.events), "thread": tid, "op": op[0], "args": args}
        )

    def _change_priority(self, tid: int) -> None:
        self.demoted = False
        for step, priority in self.change_points:
            if step == self.step:
                self.priorities[tid] = priority
                self.demoted = True

    def _finish_quantum(self, tid: int, op_name: str) -> None:
        force = self.mode == "free" or op_name in _YIELD_OPS
        if self.mode == "gil":
            self.quantum += 1
            if force or self.quantum >= self.switch_interval:
                self.gil_owner = None
                self.quantum = 0
                self.yielded = True
            else:
                self.gil_owner = tid
                self.yielded = False
        else:
            self.gil_owner = None
            self.quantum = 0
            self.yielded = True
        thread = self.threads[tid]
        if thread["kind"] == "loop":
            done = thread["pc"] >= len(thread["ops"]) or thread["status"] != "runnable"
            if op_name in ("await", "allow") or done:
                self.loop_runner = None
            else:
                self.loop_runner = tid

    def _retire_if_done(self, tid: int) -> None:
        thread = self.threads[tid]
        if thread["status"] == "runnable" and thread["pc"] >= len(thread["ops"]):
            thread["status"] = "finished"
            if self.loop_runner == tid:
                self.loop_runner = None
            for other in self.threads.values():
                if other["status"] == "blocked_join" and other["wait_for"] == tid:
                    other["status"] = "runnable"

    def _dispatch(self, tid: int, op: tuple) -> bool:
        name = op[0]
        handler = getattr(self, f"_op_{name}", None)
        if handler is None:
            raise Invariant(f"unknown op {name}")
        result = handler(tid, *op[1:])
        return True if result is None else bool(result)

    def _mover(self, tid: int, kind: str) -> None:
        stack = self.regions[tid]
        if not stack:
            return
        phase = stack[-1]["phase"]
        nxt = _next_phase(phase, kind)
        if nxt is None:
            message = f"atomic region {stack[-1]['name']} saw {kind} after {phase}"
            self.violations.append(message)
            if self.schedule.raise_atomicity:
                self.fail(AtomicityViolation, message)
            return
        stack[-1]["phase"] = nxt

    def _builtin_locked(self, tid: int, fn) -> None:
        if self._user_lock_held(tid):
            self._mover(tid, "BOTH")
            fn()
            return
        self._mover(tid, "RIGHT")
        self._mover(tid, "BOTH")
        fn()
        self._mover(tid, "LEFT")

    def _builtin_free(self, tid: int, fn) -> None:
        if self._user_lock_held(tid):
            self._mover(tid, "BOTH")
        else:
            self._mover(tid, "NON")
        fn()

    def _touch_cell(self, tid: int, name: str, write: bool) -> None:
        loc = f"cell:{name}"
        if write:
            self._ft_write(tid, loc)
        else:
            self._ft_read(tid, loc)
        self._lockset(loc, self._held_names(tid))
        if self._user_lock_held(tid):
            self._mover(tid, "BOTH")
        else:
            self._mover(tid, "NON")

    def _lockset(self, loc: str, held: set) -> None:
        current = self.locksets.get(loc)
        if current is None:
            self.locksets[loc] = set(held)
        else:
            self.locksets[loc] = current & set(held)

    def lockset_warnings(self) -> list[str]:
        return sorted(loc for loc, held in self.locksets.items() if not held)

    def _hb(self, tid: int, clock: int, other: int | None) -> bool:
        if other is None or other == tid:
            return True
        return self.clocks[tid].get(other, 0) >= clock

    def _tick(self, tid: int) -> int:
        clock = self.clocks[tid]
        clock[tid] = clock.get(tid, 0) + 1
        return clock[tid]

    def _join_clock(self, tid: int, other: dict) -> None:
        dest = self.clocks[tid]
        for who, clock in other.items():
            if clock > dest.get(who, 0):
                dest[who] = clock

    def _loc(self, name: str) -> dict:
        loc = self.locations.get(name)
        if loc is None:
            loc = {"w": (0, None), "r": ("epoch", 0, None)}
            self.locations[name] = loc
        return loc

    def _report_race(self, message: str) -> None:
        self.races.append(message)
        if self.schedule.raise_races:
            self.fail(Race, message)

    def _ft_write(self, tid: int, name: str) -> None:
        if name in self.frozen_locs:
            return
        self._tick(tid)
        loc = self._loc(name)
        write_clock, write_tid = loc["w"]
        if not self._hb(tid, write_clock, write_tid):
            self._report_race(f"write {name} by {tid} races with write by {write_tid}")
            self.frozen_locs.add(name)
            return
        kind = loc["r"][0]
        if kind == "epoch":
            self.epoch_ops += 1
            _, clock, other = loc["r"]
            if not self._hb(tid, clock, other):
                self._report_race(f"write {name} by {tid} races with read by {other}")
                self.frozen_locs.add(name)
                return
        else:
            self.vector_ops += 1
            for other, clock in loc["r"][1].items():
                if not self._hb(tid, clock, other):
                    self._report_race(f"write {name} by {tid} races with read by {other}")
                    self.frozen_locs.add(name)
                    return
        loc["w"] = (self.clocks[tid][tid], tid)
        loc["r"] = ("epoch", 0, None)

    def _ft_read(self, tid: int, name: str) -> None:
        if name in self.frozen_locs:
            return
        self._tick(tid)
        loc = self._loc(name)
        write_clock, write_tid = loc["w"]
        if not self._hb(tid, write_clock, write_tid):
            self._report_race(f"read {name} by {tid} races with write by {write_tid}")
            self.frozen_locs.add(name)
            return
        now = self.clocks[tid][tid]
        if loc["r"][0] == "epoch":
            _, clock, other = loc["r"]
            if other is None or other == tid or self._hb(tid, clock, other):
                loc["r"] = ("epoch", now, tid)
                self.epoch_ops += 1
            else:
                loc["r"] = ("vector", {other: clock, tid: now})
                self.vector_ops += 1
        else:
            loc["r"][1][tid] = now
            self.vector_ops += 1

    def _ft_acquire(self, tid: int, name: str) -> None:
        self._join_clock(tid, self.lock_vc.get(name, {}))
        self._tick(tid)

    def _ft_release(self, tid: int, name: str) -> None:
        self._tick(tid)
        self.lock_vc[name] = dict(self.clocks[tid])

    def _header(self, name: str) -> dict:
        header = self.extra["headers"][name]
        if not header["alive"]:
            self.fail(UseAfterFree, name)
        return header

    def _free_header(self, header: dict, during_stw: bool) -> None:
        header["alive"] = False
        if header.get("finalizer") == "take_user":
            if during_stw and self.scenario.finalize_in_stw:
                lock = self._ensure_lock("user", "user")
                if lock["owner"] not in (None,):
                    self.fail(Deadlock, "finalizer ran inside stop-the-world")
                lock["owner"] = 0
                self.marks.append("finalized_in_stw")
            else:
                header["deferred"] = True

    def _merge_header(self, header: dict, actor: int, during_stw: bool) -> None:
        header["local"] += header["shared"]
        header["shared"] = 0
        if header["state"] != "default":
            header["state"] = "merged"
        self.marks.append(f"merge_actor:{actor}")
        if header["immortal"]:
            header["alive"] = True
            return
        if header["local"] == 0:
            self._free_header(header, during_stw=during_stw)

    def _scan_tasks(self) -> None:
        if not self.extra.get("task_exc") or self.extra.get("task_retrieved"):
            return
        if self.extra.get("task_logged"):
            return
        live = any(
            thread["status"] == "runnable" and thread["pc"] < len(thread["ops"])
            for thread in self.threads.values()
        )
        if live:
            return
        line = "Task exception was never retrieved"
        if self.debug and self.extra.get("creation"):
            line = line + "\n" + self.extra["creation"]
        self.logs.append(line)
        self.extra["task_logged"] = True
        if self.schedule.strict_tasks:
            self.fail(NeverRetrieved, line)

    def _op_bc(self, tid: int, label: str = "") -> None:
        thread = self.threads[tid]
        if thread["kind"] == "loop":
            thread["cb_run"] += 1
            # asyncio debug mode logs callbacks longer than slow_callback_duration
            # (100 ms). This simulator counts steps, not wall time.
            if thread["cb_run"] == self.scenario.slow_callback_steps + 1:
                self.logs.append("slow callback")
        if label:
            self.marks.append(label)

    def _op_note(self, tid: int, text: str) -> None:
        self.marks.append(text)

    def _op_const(self, tid: int, value: int) -> None:
        self.acc[tid] = value

    def _op_load(self, tid: int, cell: str) -> None:
        self.acc[tid] = self.cells[cell]
        self._touch_cell(tid, cell, write=False)

    def _op_add(self, tid: int, amount: int) -> None:
        self.acc[tid] = self.acc[tid] + amount

    def _op_store(self, tid: int, cell: str) -> None:
        self.cells[cell] = self.acc[tid]
        self._touch_cell(tid, cell, write=True)

    def _op_read(self, tid: int, src: str, dest: str) -> None:
        self.cells[dest] = self.cells[src]
        self._touch_cell(tid, src, write=False)

    def _op_write(self, tid: int, cell: str, value: int) -> None:
        self.cells[cell] = value
        self._touch_cell(tid, cell, write=True)

    def _op_acq(self, tid: int, name: str, kind: str = "user") -> bool:
        lock = self._ensure_lock(name, kind)
        if lock["owner"] is None:
            lock["owner"] = tid
            self.threads[tid]["held"].add(name)
            self._ft_acquire(tid, name)
            self._mover(tid, "RIGHT")
            return True
        if lock["owner"] == tid:
            self.fail(Deadlock, f"non-reentrant acquire of {name}")
        self.threads[tid]["status"] = "blocked_lock"
        lock["waiters"].append(tid)
        return False

    def _op_rel(self, tid: int, name: str) -> None:
        lock = self._ensure_lock(name)
        if lock["owner"] != tid:
            self.fail(Invariant, f"thread {tid} does not hold {name}")
        self._ft_release(tid, name)
        self._mover(tid, "LEFT")
        lock["owner"] = None
        self.threads[tid]["held"].discard(name)
        waiters = lock["waiters"]
        lock["waiters"] = []
        for waiter in waiters:
            if self.threads[waiter]["status"] == "blocked_lock":
                self.threads[waiter]["status"] = "runnable"

    def _op_fork(self, tid: int, child: int) -> None:
        self._tick(tid)
        self.clocks.setdefault(child, {child: 0})
        self._join_clock(child, self.clocks[tid])
        self._tick(child)
        child_thread = self.threads[child]
        if child_thread["status"] == "unborn":
            child_thread["status"] = "runnable"

    def _op_join(self, tid: int, child: int) -> bool:
        child_thread = self.threads[child]
        done = child_thread["status"] in ("finished", "dead") or (
            child_thread["status"] != "unborn"
            and child_thread["pc"] >= len(child_thread["ops"])
        )
        if not done:
            self.threads[tid]["status"] = "blocked_join"
            self.threads[tid]["wait_for"] = child
            return False
        self._join_clock(tid, self.clocks[child])
        self._tick(tid)
        return True

    def _op_await(self, tid: int) -> None:
        self.threads[tid]["cb_run"] = 0

    def _op_allow(self, tid: int) -> None:
        self.threads[tid]["status"] = "blocked_io"
        self.threads[tid]["io_seen"] = False
        self.gil_owner = None
        self.yielded = True
        if self.loop_runner == tid:
            self.loop_runner = None

    def _op_atomic(self, tid: int, name: str) -> None:
        self.regions[tid].append({"name": name, "phase": "RIGHT"})

    def _op_atomic_end(self, tid: int) -> None:
        if not self.regions[tid]:
            self.fail(Invariant, "atomic_end without atomic")
        # Right-movers, then at most one non-mover, then left-movers: every
        # phase _next_phase can reach is a legal place for the region to end.
        self.regions[tid].pop()

    def _op_dict_contains(self, tid: int, name: str, key: str) -> None:
        def read() -> None:
            self.acc[tid] = 1 if key in self.extra["dicts"][name] else 0

        self._builtin_free(tid, read)

    def _op_dict_del(self, tid: int, name: str, key: str) -> None:
        def mutate() -> None:
            # ``del d[k]`` raises KeyError when another thread removed k first.
            if key not in self.extra["dicts"][name]:
                self.fail(Invariant, f"KeyError: del {name}[{key!r}]")
            del self.extra["dicts"][name][key]

        self._builtin_locked(tid, mutate)

    def _op_dict_pop(self, tid: int, name: str, key: str) -> None:
        def mutate() -> None:
            self.extra["dicts"][name].pop(key, None)

        self._builtin_locked(tid, mutate)

    def _op_dict_set(self, tid: int, name: str, key: str) -> None:
        def mutate() -> None:
            self.extra["dicts"][name][key] = self.acc[tid]

        self._builtin_locked(tid, mutate)

    def _op_dict_get(self, tid: int, name: str, key: str) -> None:
        def read() -> None:
            self.acc[tid] = self.extra["dicts"][name][key]

        self._builtin_free(tid, read)

    def _op_dict_set_str(self, tid: int, name: str, key: str) -> None:
        def mutate() -> None:
            self.marks.append("eq_under_lock")
            self.extra["dicts"][name][key] = self.acc[tid]

        self._builtin_locked(tid, mutate)

    def _op_dict_set_custom(self, tid: int, name: str) -> None:
        if not self.scenario.release_for_eq:
            self.fail(Deadlock, "per-object lock held across user __eq__")
        self.marks.append("lock_released_for_eq")
        self.extra["dicts"][name]["from_eq"] = 1
        self.extra["dicts"][name]["custom"] = self.acc[tid]

    def _op_list_len(self, tid: int, name: str, dest: str) -> None:
        def read() -> None:
            if self.extra["sorting"].get(name):
                self.cells[dest] = 0
            else:
                self.cells[dest] = len(self.extra["lists"][name])

        self._builtin_free(tid, read)

    def _op_list_get(self, tid: int, name: str, dest: str) -> None:
        def read() -> None:
            if self.extra["sorting"].get(name):
                self.cells[dest] = None
            else:
                data = self.extra["lists"][name]
                self.cells[dest] = data[0] if data else None

        self._builtin_free(tid, read)

    def _op_sort_begin(self, tid: int, name: str) -> None:
        def mutate() -> None:
            data = list(self.extra["lists"][name])
            self.extra["hidden"][name] = data
            self.extra["lists"][name] = []
            self.extra["sorting"][name] = True

        self._builtin_locked(tid, mutate)

    def _op_sort_end(self, tid: int, name: str) -> None:
        def mutate() -> None:
            hidden = self.extra["hidden"].pop(name)
            self.extra["lists"][name] = sorted(hidden)
            self.extra["sorting"][name] = False

        self._builtin_locked(tid, mutate)

    def _op_set_contains(self, tid: int, name: str, item: str) -> None:
        def read() -> None:
            self.acc[tid] = 1 if item in self.extra["sets"][name] else 0

        self._builtin_free(tid, read)

    def _op_set_remove(self, tid: int, name: str, item: str) -> None:
        def mutate() -> None:
            if item not in self.extra["sets"][name]:
                self.fail(Invariant, f"KeyError: {name}.remove({item!r})")
            self.extra["sets"][name].remove(item)

        self._builtin_locked(tid, mutate)

    def _op_set_discard(self, tid: int, name: str, item: str) -> None:
        def mutate() -> None:
            self.extra["sets"][name].discard(item)

        self._builtin_locked(tid, mutate)

    def _op_buf_length(self, tid: int, name: str) -> None:
        def read() -> None:
            self.acc[tid] = len(self.extra["bufs"][name])

        self._builtin_locked(tid, read)

    def _op_buf_getchars(self, tid: int, name: str) -> None:
        def read() -> None:
            self.acc[tid] = len(self.extra["bufs"][name])

        self._builtin_locked(tid, read)

    def _op_export_view(self, tid: int, name: str) -> None:
        self.extra["views"][name] = self.extra["views"].get(name, 0) + 1

    def _op_resize(self, tid: int, name: str, size: int) -> None:
        if self.extra["views"].get(name, 0):
            raise BufferError("bytearray.resize while a memoryview is exported")
        buf = self.extra["bufs"][name]
        if size > len(buf):
            buf.extend(b"\0" * (size - len(buf)))
        else:
            del buf[size:]

    def _op_snap_buf(self, tid: int, name: str, dest: str) -> None:
        self.extra[dest] = bytes(self.extra["bufs"][name])

    def _op_view_write(self, tid: int, name: str, index: int, value: int) -> None:
        # Memoryview slice writes do not take the bytearray per-object lock.
        self._mover(tid, "NON")
        self.extra["bufs"][name][index] = value

    def _op_register(self, tid: int) -> None:
        self.extra["port"]["registered"] = True

    def _op_take_tasks(self, tid: int) -> None:
        self.extra["port"]["queue"] = list(self.extra["port"]["tasks"])

    def _op_process(self, tid: int) -> None:
        port = self.extra["port"]
        if not port["queue"]:
            return
        task = port["queue"].pop(0)
        if port["flag"]:
            port["handled"].append(("cancel", task["id"]))
            return
        if task["cancelled"]:
            self.threads[tid]["status"] = "blocked_lock"
            port["stuck"] = True
            return
        port["handled"].append(("run", task["id"]))

    def _op_confirm(self, tid: int) -> None:
        self.marks.append("confirmed")

    def _op_cancel_begin(self, tid: int) -> None:
        port = self.extra["port"]
        port["tasks"][0]["cancelled"] = True
        if self.scenario.fuse_flag:
            port["flag"] = True

    def _op_set_flag(self, tid: int) -> None:
        self.extra["port"]["flag"] = True

    def _op_cancel_rest(self, tid: int) -> None:
        for task in self.extra["port"]["tasks"][1:]:
            task["cancelled"] = True

    def _op_call_soon(self, tid: int) -> None:
        if self.debug and tid != self.scenario.loop_tid:
            self.fail(WrongThread, "call_soon from a non-loop thread")
        self.extra["cbs"].append("direct")

    def _op_call_soon_ts(self, tid: int, label: str) -> None:
        self.extra["cbs"].append(label)

    def _op_pump(self, tid: int) -> None:
        if tid != self.scenario.loop_tid:
            self.fail(WrongThread, "pump off the loop thread")
        while self.extra["cbs"]:
            label = self.extra["cbs"].pop(0)
            self.extra["ran"].append((tid, label))

    def _op_submit(self, tid: int) -> None:
        self.extra["future"] = {"done": False, "result": None}

    def _op_coro(self, tid: int) -> None:
        if tid != self.scenario.loop_tid:
            self.fail(WrongThread, "coroutine body off the loop thread")
        future = self.extra["future"]
        future["result"] = 7
        future["done"] = True

    def _op_arm_task(self, tid: int) -> None:
        self.extra["creation"] = "create_task at scenario:never_retrieved"

    def _op_boom(self, tid: int) -> None:
        self.extra["task_exc"] = "boom"
        self.extra["task_retrieved"] = False

    def _op_await_task(self, tid: int) -> None:
        self.extra["task_retrieved"] = True
        self.extra["surfaced"] = self.extra.get("task_exc")

    def _op_enq(self, tid: int, message: str, level: int) -> None:
        self.extra["queue"].append({"message": message, "level": int(level)})
        for waiter in self.extra.get("q_waiters", []):
            thread = self.threads[waiter]
            if thread["status"] == "blocked_queue":
                thread["status"] = "runnable"
        self.extra["q_waiters"] = []

    def _op_deq(self, tid: int) -> bool:
        if not self.extra["queue"]:
            self.threads[tid]["status"] = "blocked_queue"
            self.extra.setdefault("q_waiters", []).append(tid)
            return False
        record = self.extra["queue"].pop(0)
        if (
            self.scenario.respect_handler_level
            and record["level"] < self.scenario.handler_level
        ):
            self.extra["skipped"].append(record)
        else:
            self.extra["delivered"].append(record)
        return True

    def _op_ready(self, tid: int) -> None:
        self.extra["flags"]["ready"] = True
        for waiter in self.extra.get("ready_waiters", []):
            thread = self.threads[waiter]
            if thread["status"] == "blocked_join" and thread["wait_for"] is None:
                thread["status"] = "runnable"
        self.extra["ready_waiters"] = []

    def _op_wait_ready(self, tid: int) -> bool:
        if not self.extra["flags"].get("ready"):
            self.threads[tid]["status"] = "blocked_join"
            self.threads[tid]["wait_for"] = None
            self.extra.setdefault("ready_waiters", []).append(tid)
            return False
        return True

    def _op_park(self, tid: int) -> bool:
        self.threads[tid]["status"] = "blocked_lock"
        return True

    def _op_fork_os(self, tid: int) -> None:
        if tid != 1:
            self.fail(Invariant, "fork off the main thread")
        for other, thread in self.threads.items():
            if other == tid:
                continue
            thread["status"] = "dead"
            thread["attached"] = False
        for lock in self.locks.values():
            if lock["kind"] == "python":
                lock["owner"] = None
                lock["waiters"] = []
            elif lock["kind"] == "native" and lock["owner"] not in (None, tid):
                lock["owner"] = -1
        for thread in self.threads.values():
            if thread["status"] != "dead":
                thread["held"] = {
                    name
                    for name in thread["held"]
                    if self.locks[name]["owner"] == tid
                }
        self.marks.append("forked")

    def _op_restore(self, tid: int) -> None:
        if self.threads[tid]["attached"]:
            self.fail(Deadlock, "PyEval_RestoreThread on an attached thread state")

    def _op_ensure(self, tid: int) -> None:
        if self.scenario.finalizing:
            self.fail(WrongThread, "PyGILState_Ensure during finalization")

    def _op_stw(self, tid: int) -> bool:
        self.threads[tid]["status"] = "barrier"
        attached = [
            other
            for other, thread in self.threads.items()
            if thread["attached"] and thread["status"] not in ("dead", "unborn", "finished")
        ]
        if any(self.threads[other]["status"] != "barrier" for other in attached):
            return True
        for header in self.extra["headers"].values():
            if not header["alive"] or header["deferred"]:
                continue
            if header["state"] == "queued" or header["shared"]:
                # Merging frees the header when the merged count is zero.
                self._merge_header(header, actor=tid, during_stw=True)
            elif not header["immortal"] and header["local"] == 0:
                self._free_header(header, during_stw=True)
        for other in attached:
            self.threads[other]["status"] = "runnable"
        self.marks.append("stw_release")
        deferred = any(
            header.get("deferred") and header.get("finalizer") == "take_user"
            for header in self.extra["headers"].values()
        )
        finalizer = self.threads.get(self.scenario.finalizer_tid)
        if deferred and finalizer is not None and finalizer["status"] == "unborn":
            finalizer["status"] = "runnable"
        return True

    def _op_incref_local(self, tid: int, name: str) -> None:
        header = self._header(name)
        if header["immortal"]:
            self.marks.append("immortal_noop")
            return
        if tid != header["owner"]:
            self.fail(Invariant, "local incref from a non-owner")
        header["local"] += 1

    def _op_incref_shared_atomic(self, tid: int, name: str) -> None:
        header = self._header(name)
        if header["immortal"]:
            self.marks.append("immortal_noop")
            return
        if tid != header["owner"] and header["state"] == "default":
            header["state"] = "weakrefs"
        header["shared"] += 1

    def _op_incref_shared_read(self, tid: int, name: str) -> None:
        header = self._header(name)
        if tid != header["owner"] and header["state"] == "default":
            header["state"] = "weakrefs"
        self.acc[tid] = header["shared"]

    def _op_incref_shared_write(self, tid: int, name: str) -> None:
        header = self._header(name)
        header["shared"] = self.acc[tid] + 1

    def _op_decref_shared(self, tid: int, name: str) -> None:
        header = self._header(name)
        if header["immortal"]:
            self.marks.append("immortal_noop")
            return
        header["shared"] -= 1
        if header["shared"] < 0:
            header["state"] = "queued"
            if not header["owner_alive"]:
                self._merge_header(header, actor=tid, during_stw=False)
            return
        if header["local"] + header["shared"] == 0:
            self._free_header(header, during_stw=False)

    def _op_touch(self, tid: int, name: str) -> None:
        self._header(name)
        self.marks.append(f"touch:{name}")


def _next_phase(phase: str, kind: str) -> str | None:
    if phase == "RIGHT":
        if kind in ("RIGHT", "BOTH"):
            return "RIGHT"
        if kind == "NON":
            return "NON"
        if kind == "LEFT":
            return "LEFT"
    elif phase == "NON":
        if kind in ("LEFT", "BOTH"):
            return "LEFT"
        return None
    elif phase == "LEFT":
        if kind in ("LEFT", "BOTH"):
            return "LEFT"
        return None
    return None


def _check_schedule(schedule: Schedule) -> None:
    if schedule.mode not in ("gil", "free"):
        raise ValueError("mode must be gil or free")
    if schedule.policy not in ("pct", "replay", "guide", "search"):
        raise ValueError(f"unknown policy {schedule.policy}")
    for name in ("depth", "n_max", "k_budget", "switch_interval"):
        value = getattr(schedule, name)
        if not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive int")


def run(scenario: Scenario, schedule: Schedule) -> Trace:
    """Run one schedule. Replay runs the journal prefix, then the rest of the program.

    A repaired program can have steps left after the recorded failure point.
    Those run under the seeded priorities, so "completes" means every thread
    finished, not only that the prefix matched.
    """
    _check_schedule(schedule)
    world = World(scenario, schedule)
    while True:
        if schedule.policy == "replay" and world.replay_i < len(schedule.replay_events or []):
            events = schedule.replay_events or []
            world._wake_io(force=False)
            event = events[world.replay_i]
            tid = event["thread"]
            if tid not in world.enabled() and world._pending_io():
                world._wake_io(force=True)
            if tid not in world.enabled():
                world.fail(
                    ReplayDivergence,
                    f"recorded thread {tid} is not enabled at event {world.replay_i}",
                )
            before = len(world.events)
            world.step_thread(tid)
            if len(world.events) == before:
                world.fail(
                    ReplayDivergence,
                    f"recorded thread {tid} did not complete event {world.replay_i}",
                )
            actual = world.events[-1]
            if actual["op"] != event["op"] or list(actual["args"]) != list(event.get("args", [])):
                world.fail(
                    ReplayDivergence,
                    f"event {world.replay_i} ran {actual['op']}{actual['args']}",
                )
            world.replay_i += 1
            continue
        if schedule.policy == "guide":
            guide = schedule.guide or []
            if world.guide_i >= len(guide):
                break
            world._wake_io(force=False)
            tid = guide[world.guide_i]
            if tid not in world.enabled() and world._pending_io():
                world._wake_io(force=True)
            if tid not in world.enabled():
                world.fail(ReplayDivergence, f"guided thread {tid} is not enabled")
            before = len(world.events)
            world.step_thread(tid)
            if len(world.events) == before:
                world.fail(ReplayDivergence, f"guided thread {tid} did not complete a step")
            world.guide_i += 1
            continue
        world._wake_io(force=False)
        tid = world.pick_highest(world.enabled()) if world.enabled() else None
        if tid is None:
            if world._pending_io():
                world._wake_io(force=True)
                continue
            if world.deadlocked():
                world.fail(Deadlock, scenario.invariant_id or scenario.name)
            break
        world.step_thread(tid)
    if schedule.policy == "guide" and world.deadlocked():
        world.fail(Deadlock, scenario.invariant_id or scenario.name)
    world._scan_tasks()
    if scenario.holds is not None and not scenario.holds(world):
        world.fail(Invariant, scenario.invariant_id or scenario.name)
    trace = world.trace(failure=None)
    log.info(
        "scenario=%s variant=%s failure=%s trace=%s",
        scenario.name,
        scenario.variant,
        "none",
        trace.sha256,
    )
    return trace


@dataclass
class SearchHit:
    bound: int
    journal: dict


def search_first(scenario: Scenario, schedule: Schedule, max_bound: int = 2) -> SearchHit | None:
    """CHESS-style bound walk. A PCT priority change may switch at bound 0.

    Voluntary switches spend budget. The walk stops at the first deadlock.
    ``schedule`` is read, not modified; the hit carries its own bound.
    """
    _check_schedule(schedule)
    if max_bound < 0:
        raise ValueError("max_bound must be >= 0")
    for bound in range(max_bound + 1):
        hit = _search_bound(scenario, schedule, bound)
        if hit is not None:
            return hit
    return None


def _search_bound(scenario: Scenario, schedule: Schedule, bound: int) -> SearchHit | None:
    found: dict = {}

    def walk(world: World, budget: int, current: int | None) -> None:
        if found:
            return
        if world.step > schedule.k_budget:
            return
        world._wake_io(force=False)
        if not world.enabled() and world._pending_io():
            world._wake_io(force=True)
        natural = world.natural(current)
        if natural is None:
            if world.deadlocked():
                found["journal"] = world.journal_dict("Deadlock")
            return
        options = [(natural, budget)]
        enabled = world.enabled()
        if current in enabled and world.yielded and budget > 0:
            for tid in sorted(enabled):
                if tid != current:
                    options.append((tid, budget - 1))
        seen = set()
        for tid, next_budget in options:
            if found:
                return
            key = (tid, next_budget)
            if key in seen:
                continue
            seen.add(key)
            child = world.clone()
            before = (
                child.step,
                child.threads[tid]["pc"],
                child.threads[tid]["status"],
            )
            try:
                child.step_thread(tid)
            except Deadlock as exc:
                found["journal"] = exc.journal
                return
            except LabError:
                return
            after = (
                child.step,
                child.threads[tid]["pc"],
                child.threads[tid]["status"],
            )
            if after == before:
                return
            walk(child, next_budget, tid)

    probe = Schedule(
        mode=schedule.mode,
        seed=schedule.seed,
        depth=schedule.depth,
        n_max=schedule.n_max,
        k_budget=schedule.k_budget,
        preemption_bound=bound,
        switch_interval=schedule.switch_interval,
        policy="search",
        priorities=schedule.priorities,
        change_points=[] if schedule.change_points is None else schedule.change_points,
        raise_races=False,
        raise_atomicity=False,
    )
    # Empty change points keep the cancel search on voluntary preemptions.
    # A PCT campaign draws its own change points and does not use this walk.
    root = World(scenario, probe)
    walk(root, bound, None)
    if not found:
        return None
    return SearchHit(bound=bound, journal=found["journal"])


def epoch_ratio(trace: Trace) -> float:
    total = trace.epoch_ops + trace.vector_ops
    if total == 0:
        return 1.0
    return trace.epoch_ops / total
