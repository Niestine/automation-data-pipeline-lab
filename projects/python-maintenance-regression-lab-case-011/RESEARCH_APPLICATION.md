# Research application

Nine public sources shape this lab. Each one below is wired into the interpreter or the tests. Titles link to the public pages. The archived extracts used while designing the lab are windows, and two gaps are named where the implementation follows the source card because the extract stops early.

## Probabilistic concurrency testing

Burckhardt, Kothari, Musuvathi, and Nagarakatte, [A Randomized Scheduler with Probabilistic Guarantees of Finding Bugs](https://www.microsoft.com/en-us/research/publication/a-randomized-scheduler-with-probabilistic-guarantees-of-finding-bugs/) (ASPLOS 2010). PDF: [asplos277-pct.pdf](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/asplos277-pct.pdf).

`yieldlab.engine` assigns each thread a priority `depth + rank - 1` from a seed-shuffled permutation. The higher number runs. A tie breaks toward the smaller thread id. Depth `d` draws `d - 1` change points uniformly from steps `1 .. k_budget`. After the step whose length equals a change point, the thread that just ran is rewritten to priority `d - i`. Depth 1 has no change points. `k_budget` counts yield steps. A run of exactly that many steps is legal; the next step raises `BoundExceeded`. A scenario with more threads than `n_max` is refused before it starts.

The suite treats the paper bound `1 / (n * k^(d-1))` as a target and requires the measured rate to clear half of it. Depth 1 on the four-thread ordering bug uses 80 seeds and floor `1/8`. Depth 2 on the two-thread lost update uses `k = 6`, 240 seeds, and floor `1/24`. `python run_lab.py campaign` prints the paper floor next to the observed rate. A run that passes `k_budget` or `n_max` is a setup error and stops the campaign; it is never counted as a hit, because the bound only holds inside those caps. The same seed reproduces the same event list. Published hit rates from the paper's C and C++ runs are outside this lab's measurements.

## Preemption bounding and replay

Musuvathi, Qadeer, Ball, Basler, Nainar, and Neamtiu, [Finding and Reproducing Heisenbugs in Concurrent Programs](https://www.microsoft.com/en-us/research/publication/finding-and-reproducing-heisenbugs-in-concurrent-programs/) (OSDI 2008). PDF: [osdi2008-CHESS.pdf](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/osdi2008-CHESS.pdf).

`search_first` walks preemption bounds 0, then 1, then 2, and stops at the first deadlock. The first step picks the lowest enabled thread id and stays there until a voluntary switch. Each extra switch spends one unit of budget. A priority change from the PCT policy can reorder a run even when this bound is 0; the two policies are separate.

The cancel scenario follows the paper's worker-and-cancel shape on the synthetic port `northline-spur-4`, which has two tasks. The unfixed program marks the first task cancelled and stores the in-progress flag on a later step. Bounds 0 and 1 miss that window. Bound 2 records `Deadlock` in `regressions/cancel_unfixed.schedule.json`, and a fresh search with the same inputs rebuilds that journal exactly. The repair sets the flag inside `cancel_begin` and keeps the operation names, so the saved six events replay under `free` and under `gil` with `switch_interval` 1. Replay then runs the steps the repaired worker still has, and the run ends with both tasks cancelled and `confirm` recorded. Replaying a thread that is not enabled raises `ReplayDivergence`.

The ordering journal is a witness of the unfixed program. Its repair publishes the reader with `fork` after the write, which changes which threads are enabled, so that journal stays on the unfixed variant. Twenty replays of it raise `Invariant` and keep one `trace_sha256`. The hash is SHA-256 of the canonical JSON of the event list alone. `load_journal` rejects a bad shape and a hash that does not match the events.

## Epoch race detection

Flanagan and Freund, [FastTrack: Efficient and Precise Dynamic Race Detection](https://users.soe.ucsc.edu/~cormac/papers/pldi09.pdf) (PLDI 2009).

The oracle watches user cells. Operations that already hold a per-object builtin lock stay outside it. An access records an epoch, the pair `(clock, thread)`, until the location is read by concurrent readers, when the state widens to a vector clock. A later exclusive write can collapse that vector back to an epoch. After a reported race the location freezes.

Program order ticks the thread clock. `fork` copies the parent clock into the child. `join` copies the child clock into the parent. A lock release stores the releaser's clock, and a later acquire of the same lock joins it. That edge orders only what follows the acquire. In `test_acquire_does_not_hide_a_later_write`, thread 1 acquires, reads, and releases, then thread 2 writes, then thread 2 acquires and releases. The write comes before thread 2's acquire, so the oracle reports `Race`. In `test_a_different_lock_does_not_order_the_cell`, a write under lock `a` and a read under lock `b` also race. Concurrent readers of one cell do not race. A fork/join publication with no lock stays silent while the lockset computed beside it warns; the lockset warning is never the pass or fail signal. Two forked children reading one cell widen it to a vector, and the parent's write after both joins collapses it back to an epoch. That fixture has exactly two vector operations. On the catalog's ordering and lost-update runs, every classified access stays on the epoch path. The paper's Java figure (about 96 percent) is not a measurement of this lab.

## Atomicity reduction

Flanagan, Freund, and Qadeer, [Atomizer: A Dynamic Atomicity Checker for Multithreaded Programs](https://users.soe.ucsc.edu/~cormac/papers/scp08.pdf) (Science of Computer Programming, 2008).

A region marked atomic must reduce to right-movers, then at most one non-mover, then left-movers. A user mutex acquire is a right-mover and the matching release is a left-mover. A self-contained builtin with no user lock expands to right, both, left, and that expansion does not cover the next bytecode. A lock-free read with no user lock is a non-mover. `length` then `getChars` puts a new right-mover after a left-mover, and `contains` then `del` puts a right-mover after a non-mover. Both raise `AtomicityViolation` on a serial run. `dict_pop` and set `discard` are one builtin and reduce. One user lock around `length` and `getChars` reduces. An acquire, release, and second acquire inside one region is rejected. An unsynchronized load, add, and store inside one region is rejected.

The archived extract of this paper ends before the formal mover section. The phase names in the interpreter follow the reduction stated with the public paper: right-movers, an optional non-mover, then left-movers. The tests check the transitions this interpreter implements. A green reduction covers the executed schedule.

## Optional GIL, per-object locks, and reference headers

[PEP 703 – Making the Global Interpreter Lock Optional in CPython](https://peps.python.org/pep-0703/).

Every catalog scenario can run in `gil` or `free`. GIL mode keeps the owner for `switch_interval` steps unless the operation detaches. Free mode yields after every step. The unfixed lost update is three steps. A seed that fails in free mode and in GIL mode with interval 1 still passes in GIL mode with interval 100. The locked variant, one user lock around the three steps, passes both modes with `count == 2` and an empty race list. Passing GIL mode is a separate result from passing free mode.

The object header models the biased count from the PEP at lab scale. An owner-local increment is a plain add. A non-owner shared increment is one atomic step and moves the state from `default` toward `weakrefs`. An immortal object ignores both. In the `refcount` scenario, threads 1 and 2 each start with one shared reference (`shared == 2`, owner on thread 3). Each takes a temporary reference and drops it, and thread 1 then drops its own. Every serial order ends with `shared == 1`. In the unfixed variant the shared increment is a torn read and write. When both reads come before both writes, one increment is lost, and thread 2's decref frees the object while thread 2 still holds a reference. Its next touch raises `UseAfterFree`. The fixed variant makes each increment one atomic step. Eighty depth-3 PCT seeds keep the header alive with `shared == 1` and state `weakrefs`, while some of the same seeds fail the unfixed variant. A negative shared count queues a merge that runs during the next stop-the-world pause. When the owner is already dead, the acting thread merges immediately.

Stop-the-world pauses attached threads that are still alive. Threads that are dead, unborn, or finished are not barrier participants. Queued counts merge during the pause. The unfixed finalizer takes a user lock inside the pause and deadlocks when another paused thread holds that lock. The fixed path defers the finalizer until after the barrier releases, then lets the unborn thread acquire the lock and record `finalized`.

## Builtin atomicity catalog

[Thread Safety Guarantees](https://docs.python.org/3/builtins/threadsafety.html) (Python 3.14 built-ins).

One builtin call is one interpreter step. A statement such as `d[k] = d[k] + 1` is written as three ops (`dict_get`, `add`, `dict_set`). An interleaved pair of those statements loses an increment, and the serial pair does not. `d.pop` is one step. The implemented lock-free reads (dict get and contains, list len and getitem, set contains) do not take the per-object lock. `buf_length` and `buf_getchars` model the Atomizer paper's synchronized `StringBuffer.length` and `getChars`, so each takes the per-object lock for its own step. The page's other lock-free reads (dict and set `len`, bytearray `len`) are not modeled.

`contains` followed by `del` is an atomicity violation; `dict_pop` is one step. When two threads both pass the check before either deletes, the second `del` raises `KeyError`, which the lab classifies as `Invariant`. Two concurrent `dict_pop` calls do not fail. Set `contains` followed by `remove` is a violation; `discard` is one step. `list.sort` publishes an empty list at `sort_begin` and the sorted list at `sort_end`, so a concurrent length or getitem can observe `0` or `None`. An external lock covers that window. Memoryview slice writes are per-byte non-movers and do not take the bytearray lock, so a snapshot in the middle sees bytes from both writers. When both writers and the reader take one external lock, sixty depth-3 seeds only ever see whole-buffer states. The same seeds without the lock produce torn snapshots, and the sort test uses the same control. Resizing a bytearray while a view is exported raises `BufferError`. A custom `__eq__` drops the per-object lock before the callback; holding that lock across the callback deadlocks the same thread. String equality stays under the lock. A dict write is one event, so a racing reader sees the old value or the new value.

## Thread state, the GIL, and fork

[Thread states and the global interpreter lock](https://docs.python.org/3/c-api/threads.html).

`allow` detaches the caller so a partner can run in the gap. A loop callback with no `await` keeps the loop thread and the partner stays out. `fork_os` is legal only on thread 1. Other threads disappear. A Python lock owned by a dead thread is reset, so the child acquires it. A native lock keeps a dead owner, and the child's acquire deadlocks. `RestoreThread` on a thread that is already attached deadlocks. `PyGILState_Ensure` while the scenario is finalizing raises `WrongThread`. Stop-the-world is the suspension point used when the GIL is absent.

`switch_interval` is a parameter of the schedule. The suite uses 1 and 100 as explicit quanta. It does not treat either value as a default read off the documentation page.

## Asyncio debug and cooperative yield

[Developing with asyncio](https://docs.python.org/3/library/asyncio-dev.html).

The loop runs on one thread. A task runs until `await`. In debug mode, `call_soon` from a worker raises `WrongThread`. `call_soon_threadsafe` followed by a pump runs the callback on the loop thread. `run_coroutine_threadsafe` is modeled as a submit plus a coroutine that stores the future result. A task whose exception is never retrieved logs `Task exception was never retrieved`, and debug mode appends the creation site. With `strict_tasks` the same case raises `NeverRetrieved`. Awaiting the task marks it retrieved and surfaces the exception without that log line.

A callback longer than `slow_callback_steps` is marked slow. The documentation's 100 ms threshold is the real-time meaning of that cutoff. The simulator counts steps, and the comment next to the check records the correspondence.

`cursor_rows` without `aclosing` raises `Invariant` with id `cursor_rows_shutdown`. With `aclosing`, the inner generator closes first and the run passes. A second `asend` while the generator is running raises `GeneratorReentered` with the message `anext(): asynchronous generator is already running`. A generator primed before the loop starts is not registered, and `finalize` raises `RuntimeError` containing `GeneratorExit`. A generator created and primed inside a started loop is closed by `shutdown_asyncgens`.

This lab does not implement a loop-local `asyncio.Queue`. That rule lives on a sibling documentation page that was not part of the design sources used here.

## Logging lock order and the queue repair

[logging — Logging facility for Python](https://docs.python.org/3/library/logging.html).

The sequential model shares loggers by name from `getLogger`. Handlers change through `addHandler` and `removeHandler`. With `propagate` true, a handler attached to both a child and the root is delivered twice. The first module-level `info` call on a root that has no handlers runs `basicConfig` and appends the card's lock order, module then handler, to `lock_order`. The sequential model takes no real locks. The order is exercised under contention only in the `log_deadlock` scenario.

The concurrent scenario is separate. One thread acquires the handler lock and then the module lock. The other acquires the module lock and then the handler lock. Bound 0 misses it. Bound 1 finds `Deadlock` with one switch after the emit takes the handler lock. The repair is `log_queue`: the caller enqueues, one listener dequeues, and `respect_handler_level` is read from `examples/level_batch.json`. Handler level 30 skips `tick-low` at level 20 and delivers `tick-high` at level 40, on twenty depth-2 seeds, and the queue search stays clean through bound 2. With `respect_handler_level` off, both records are delivered. The lab models the enqueue/dequeue split, not the lock held inside `QueueHandler.emit`.

The archived extract of the logging page ends inside the `Logger.warning` example, before the Thread Safety section. The two-lock order, the first-call `basicConfig` behavior, and the queue-listener repair follow the source card for that page. `loggingx` records that gap in its module docstring.

## What a green run means

A maintained scenario is green when the saved journal still classifies the old failure, the repaired program completes on that journal or on a fresh seeded run, FastTrack and Atomizer stay quiet on the executed steps, and the same repair holds in `gil` and in `free` where the scenario has both variants. The oracles describe the schedule that ran. They do not certify schedules the search skipped.
