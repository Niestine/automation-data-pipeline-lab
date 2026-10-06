# Python Maintenance & Regression Lab — Case 011

`yieldlab` is a deterministic, in-process yield-point simulator for a small layered service. It replays one schedule at a time, classifies the failure, and keeps the failing interleaving as a journal. Time is a step counter. There is no sleep, no live socket, and no OS thread in the test gate.

The service under maintenance is synthetic. Port tasks come from `examples/port_tasks.json` (`northline-spur-4`, tasks `scan-a` and `scan-b`). Log records come from `examples/level_batch.json`. Nothing in those files is client data.

## Problem

Several defects only show up when a yield lands inside a compound action:

- A reader runs before the writer that was supposed to publish the value.
- Two threads load, add, and store one counter, and one update disappears. A long GIL quantum hides the window. A one-step quantum and free-threaded mode both expose it.
- A cancel marks the first task and stores the in-progress flag on a later step. The worker processes a cancelled task and deadlocks.
- Check-then-act on a dict or a set, a `list.sort` that publishes an empty list in the middle, and a memoryview write that tears one byte at a time.
- A torn shared reference-count update frees an object another thread still touches. A finalizer that runs inside stop-the-world deadlocks on a lock held by a paused thread.
- Logging configuration and a re-entering emit take the module lock and the handler lock in opposite orders.
- An asyncio task runs until `await`, a debug-mode `call_soon` arrives from the wrong thread, a failed task is never retrieved, and an async generator is shut down in the wrong order.

Each defect has a variant that removes that window, plus a test that still fails on the old program.

## Architecture

```
run_lab.py
src/yieldlab/
  engine.py      one-thread-at-a-time interpreter, PCT, replay, search
  scenarios.py   planted programs and the two JSON fixtures
  journal.py     append-only schedule files and hash checks
  campaign.py    seeded PCT samples
  loggingx.py    logger tree, propagation, first-call basicConfig
  agen.py        async-generator shutdown and re-entrancy
  cli.py         list, run, search, replay, campaign
tests/           unittest, offline, stdlib only
regressions/     saved failing schedules
examples/        synthetic port tasks and log levels
```

`run(scenario, schedule)` returns a `Trace` or raises a classified error: `Deadlock`, `Invariant`, `UseAfterFree`, `WrongThread`, `ReplayDivergence`, or `BoundExceeded`. Races and atomicity violations are recorded on `trace.races` and `trace.violations`; they raise `Race` and `AtomicityViolation` when the schedule sets `raise_races` or `raise_atomicity`. A never-retrieved task exception is a log line, or `NeverRetrieved` with `strict_tasks`. The async-generator model in `agen.py` raises `GeneratorReentered`.

The scheduler has four policies.

| Policy | What it does |
| --- | --- |
| `pct` | Highest priority enabled thread. Depth `d` draws `d - 1` priority-change points. |
| `search` | Preemption bounds 0, then 1, then 2. Stops at the first deadlock. |
| `replay` | The recorded thread must be enabled and the operation must match. After the recorded prefix, any steps the program still has run under the seeded priorities. |
| `guide` | A scripted thread list, used by tests that need one interleaving. |

GIL mode keeps the owner for `switch_interval` steps unless the operation detaches (`allow`, `await`, `stw`, `fork_os`). Free mode yields after every step. A builtin that needs the per-object lock holds it for that step only. Lock-free reads do not take it.

User cells go through an epoch race checker. A lock release stores the releaser's clock, and a later acquire of the same lock joins it. Regions marked atomic must reduce to right-movers, then at most one non-mover, then left-movers. Builtin-locked dict buckets are outside the race checker. A single builtin call expands to its own acquire, body, and release, so the next bytecode is a new region.

Journals record mode, seed, depth, preemption bound, switch interval, yield budget, priorities, change points, events, failure class, invariant id, and `trace_sha256`. The hash covers the event list only. Loading a journal recomputes the hash and rejects a mismatch.

The logger named `yieldlab` attaches a `NullHandler` on import, so the library is silent until an application configures logging. The CLI does that, at `WARNING` unless `--verbose` is set. Every finished or failed run emits one `INFO` line with scenario, variant, failure class, and trace hash; a test captures those lines with `assertLogs`. CLI input errors (unknown scenario or variant, a bad journal, a campaign outside its bound) print `yieldlab: error: ...` to stderr and exit 2.

## Run

From the repository root, with Python 3.10 or newer and no third-party packages:

```
python -m unittest discover -s projects/python-maintenance-regression-lab-case-011/tests -v
```

The CLI inserts its own `src` directory:

```
python projects/python-maintenance-regression-lab-case-011/run_lab.py list
python projects/python-maintenance-regression-lab-case-011/run_lab.py run ordering_d1 --variant unfixed --dry-run
python projects/python-maintenance-regression-lab-case-011/run_lab.py search cancel_port --variant unfixed --bound 2
python projects/python-maintenance-regression-lab-case-011/run_lab.py replay projects/python-maintenance-regression-lab-case-011/regressions/cancel_unfixed.schedule.json
python projects/python-maintenance-regression-lab-case-011/run_lab.py campaign lost_update --variant unfixed --depth 2 --sample 240 --k-budget 6 --n-max 2
```

`--dry-run` prints the scenario name, variant, thread count, step count, and mode, then returns 0. It does not execute the steps. `search` prints the failure class, bound, trace hash, and step count. `campaign` prints hits, sample size, observed rate, and the paper floor `1 / (n_max * k_budget^(depth-1))`. A run that passes `k_budget` or `n_max` stops the campaign with an error instead of counting as a hit, because the floor only holds inside those caps. The campaign defaults (`--k-budget 4 --n-max 4`) fit `ordering_d1`; `lost_update` needs `--k-budget 6` or more.

Catalog names from `list`: `cancel_port`, `finalizer_stw`, `log_deadlock`, `log_queue`, `lost_update`, `ordering_d1`, `refcount`.

## Design decisions

The maintenance loop is replay, confirm, repair, rerun.

1. Replay the saved journal and confirm the failure class and `trace_sha256`.
2. Repair the program. When the journal has to survive, keep the operation names. The cancel repair sets the flag inside `cancel_begin` for that reason.
3. Run the repaired program in `gil` and in `free` where both modes apply.
4. Leave the old journal in `regressions/`. A replay that diverges has not preserved the bug.

The ordering repair is different. The fixed program forks the reader after the write, so the unfixed journal is a witness of the old program. Forty PCT seeds of the fixed program stay quiet at depth 1 and at depth 2, and the race list is empty.

PCT priorities follow the ASPLOS 2010 scheduler: higher numbers win, and a change point rewrites the thread that just ran. The depth-1 and depth-2 tests require the observed hit rate to clear half the paper bound on 80 and 240 seeds. The campaign command prints the full paper floor. A priority change can reorder a PCT run even when the preemption bound is 0, because search and PCT are different policies.

`k_budget` is the yield budget. Six yields are the lost-update budget used by the depth-2 sample, and a seventh yield raises `BoundExceeded`.

Stop-the-world waits for attached threads that are still alive. Unborn, finished, and dead threads are not barrier participants, so the deferred finalizer thread can be enabled after the pause.

## What it demonstrates

- Seeded schedules. The same seed on the locked counter produces the same event list and the same hash.
- A depth-1 ordering bug and a depth-2 lost update, with rates above the half-bound floors, and quiet repaired variants. The locked update has zero hits over 240 depth-2 seeds in both modes.
- Exact replay. Twenty replays of `regressions/ordering_d1_unfixed.schedule.json` raise `Invariant` and share one hash. The cancel journal deadlocks on `unfixed`. On `fixed`, in `free` and in `gil` with interval 1, the same six events replay and the worker then finishes both tasks and `confirm`.
- Search bounds. Cancel and the logging lock inversion are missed at bound 0. Cancel is also missed at bound 1, and bound 2 records that deadlock; a fresh search rebuilds the saved journal exactly. The logging inversion is found at bound 1. The fixed cancel and the queue repair stay clean through bound 2.
- Compatibility of the two runtime modes. The unlocked three-step update can pass a 100-step GIL quantum and fail when the quantum is one step. The locked update passes both modes. In GIL mode a thread keeps the GIL through its quantum unless it reaches `allow`.
- Epoch races versus lockset warnings: a write placed before an acquire is still reported, a critical section on a different lock does not order the cell, and a fork/join publication is quiet even though the lockset warns.
- Atomicity failures that a race checker stays quiet on: `length` then `getChars` on one thread, dict contains-then-delete, set contains-then-remove. With two threads, contains-then-delete also crashes with a `KeyError` (classified as `Invariant`); `dict_pop` does not.
- Builtin windows: sort, memoryview tear, exported-buffer resize, custom equality that must drop the per-object lock. The locked sort and memoryview tests sweep seeded schedules that do tear when the lock is removed.
- Reference counts: a torn shared increment that serial schedules survive and an interleaving turns into a use-after-free, immortal no-ops, a queued merge during stop-the-world, a dead-owner merge, and a finalizer deferred until after the pause.
- Fork lock reset. A Python lock can be taken in the child. A native lock owned by a dead thread cannot. Restoring an attached thread deadlocks. Ensuring the GIL during finalization raises `WrongThread`.
- Asyncio cooperation, wrong-thread scheduling, never-retrieved task exceptions, slow-callback step counts, and async-generator shutdown.
- Logging propagation, first-call `basicConfig`, the handler/module deadlock, and a queue listener that honors the handler level. The library's own `logging` output is tested as well.
- Defensive fixture parsing. A port file needs at least two tasks. A level file needs an integer handler level and a non-empty record list. Bad journals fail in `load_journal`.

## Limitations

The interpreter runs scripted operations. It does not execute CPython bytecode, start OS threads, or call `os.fork`. A green race report and a green reduction describe the schedule that ran.

`switch_interval` is a schedule parameter. The values 1 and 100 are test quanta.

The Atomizer extract used for this design ends before the formal mover section. The checker implements right-movers, an optional non-mover, and left-movers, and the tests cover those transitions. The logging extract ends before the Thread Safety section. The two-lock order and the queue repair are implemented from the public page's source card, and `loggingx` says so. The lab does not implement a loop-local `asyncio.Queue`.

The builtin catalog covers the operations the tests call. It does not encode every set-algebra rule on the documentation page. There are no subinterpreters. The reference-count header uses the PEP 703 states this scenario needs. It is a scaled model, and the suite is the spec.

Paper bug counts and speedups are not results of this lab. The depth tests report whether this scheduler cleared its own floor.
