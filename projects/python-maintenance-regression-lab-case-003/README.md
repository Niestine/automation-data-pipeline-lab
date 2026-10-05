# Python Maintenance & Regression Lab — Case 003

A single-process concurrency lab. It runs one instrumented operation of one logical thread at a time, records the thread ids, and uses that schedule as the regression test. The buggy twin and the fixed twin share yield points on the two data races, so the same thread sequence is still feasible after the fix. The deadlock fix changes lock order, so the old sequence is allowed to come back `diverged` only when a bounded search is clean.

Nothing here calls a network service. Inputs are the planted subjects. OS threads are an informational smoke and are not the gate. Standard library only; Python 3.9 or newer.

## Problem

A green stress run is a weak maintenance signal:

- an ordering bug (read the payload before its ready flag) shows up on only some interleavings
- a check-then-act bug stores a stale snapshot; both threads can read 0 and both can write 1
- two locks taken in opposite orders deadlock on one preemption and run cleanly on the baseline
- a scheduler that sleeps, reads the clock, or draws from the global RNG is not replaying the schedule it claims to replay
- a starved enabled thread is not a deadlock
- a DEBUG trace of every step is for a person; the file CI replays is the JSON schedule

The suite is the maintenance contract for those distinctions.

## Architecture

```text
subject ops (buggy or fixed)
  -> one interpreter step: read, write, write_add, rmw_add, acquire, release, read_if
  -> enabled set (not finished, not blocked)
  -> policy: pct | preemption | delay | random | dfs | fair
  -> oracle: pass | fail | deadlock | step-cap | bound_exceeded | diverged | cap
  -> JSON schedule + a per-run DEBUG log
  -> replay forces the recorded tids and ignores priorities
```

Package `src/schedlab`:

- `machine.py` — logical threads, integer cells, locks. One op per step. Re-entry raises
- `policies.py` — PCT, iterative preemption bounding, iterative delay bounding, controlled random, DFS, fair round-robin
- `replay.py` — force a recorded tid list
- `oracles.py` — which terminals count as a subject failure, and the data-race and deadlock replay gates
- `campaign.py` — seeded repeats, first-failure counts, the exact controlled-random failure rate, the trivial screen, the PCT run budget
- `subjects.py` — `ordering_d1`, `atomicity_d2`, `deadlock_d2`
- `artifact.py` — schema 1 JSON load and store
- `logsetup.py` — DEBUG file, INFO console
- `environment.py` — GIL stamp, read once per process
- `native.py` — informational OS-thread smoke, writes no artifact

A step is a scheduling point on a cell or a lock. A plain `list.append` outside the interpreter does not advance the step counter. The lab makes no claim about races inside CPython containers.

Lock handoff: a blocking `acquire` is one step and consumes that op. `release` gives the lock to the lowest-tid waiter. The waiter does not spend a second step to become the holder. Because of that, `k` for a planted subject is the number of ops in its lists. A deadlock still stops early.

## Planted subjects

| Subject | Bug | Buggy shape | Fixed shape |
| --- | --- | --- | --- |
| `ordering_d1` | depth 1, use before init | thread 2 reads `value` then `ready` | thread 2 reads `ready` and skips the payload when the flag is clear |
| `atomicity_d2` | depth 2, stale snapshot | read `x`, later store `snapshot + 1` | read `x`, later increment the live cell |
| `deadlock_d2` | depth 2, opposite lock order | thread 1 takes `a` then `b`; thread 2 takes `b` then `a` | both take `a` then `b` |

Checked-in schedules under `examples/`:

- `ordering_d1_buggy.json` — `[2, 2, 1, 1]`. Buggy `observed` is 0 (`fail`). Fixed sees `ready == 0` and sets `observed` to `skip` (`pass`).
- `atomicity_d2_buggy.json` — `[1, 2, 1, 2]`. Buggy ends at `x == 1` (`fail`). Fixed ends at `x == 2` (`pass`).
- `deadlock_d2_buggy.json` — `[1, 2, 2, 1]`. Buggy replays to `deadlock` with one preemption. Fixed replays to `diverged`.

These three files are hand-written fixtures, not campaign output. Their `gil` and `python` fields hold the placeholder `fixture` instead of a real interpreter stamp. `tests/test_regressions.py` checks their `steps`, `preemptions`, `delays`, `terminal`, and `final_cells` against a fresh replay. A replay run with `--artifact-dir` writes a new artifact that carries the real stamp.

A third tid that is not enabled (`[1, 2, 9]` on the atomicity subject) is `diverged`. For the two data races, `diverged` fails the fixture. For the deadlock fixture, `diverged` passes only together with a preemption search through bound 5 that reports no deadlock. A fixed replay that still deadlocks fails the fix.

## Policies

PCT assigns a distinct random priority at thread creation from `random.Random(seed)`, always runs the highest-priority enabled thread, and after each of `d - 1` change points lowers the thread that just ran strictly below every other thread. Depth 1 draws no change points. Campaigns for `d = 1`, `d = 2`, and `d = 3` stay in separate rows. The per-run floor used for the budget is `1 / (n * k ** (d - 1))` for that depth only. `budget` prints the smallest `R` with `(1 - p) ** R <= 0.01`. A run that creates more than `n_max` threads, or that is asked to take step `k + 1`, is `bound_exceeded` and is not a probability claim.

Preemption bounding and delay bounding search bound 0, then 1, and so on through 5. A preemption is a switch away from a thread that is still enabled. The first dispatch is free. A switch after a block or a finish costs nothing. A delay is a choice other than the lowest enabled tid. Delay bound 0 is that baseline and nothing else. A finished bound-`c` search supports only: any bug still hidden needs at least `c + 1` preemptions, or delays. The coverage text is `no failure within bound c`. It does not say the program is bug free.

Controlled random picks uniformly among enabled tids and records the choice. DFS tries enabled tids in ascending order. Fair picks the enabled thread with the smallest `last_step`, then the lowest tid. PCT is allowed to leave an enabled thread unrun; that terminal is `step-cap`, not `deadlock`.

### Trivial-bug screen

A subject is `trivial` when controlled random fails it on at least half of its runs. `exact_failure_rate` computes that probability exactly. It walks every choice `run_random` could make, weights each by `1 / len(enabled)`, and memoises on the machine state. The screen also runs the fixed seeds `0` through `29` (`examples/screen_seeds.json`) and reports the sampled `hits` beside the exact rate. The exact rate decides the tag. Every unpadded bug here sits at exactly one half, so a 30-seed sample would flip the tag on noise.

| Subject | Exact random failure rate | Hits on seeds 0–29 | Tag |
| --- | --- | --- | --- |
| `ordering_d1` | 1/2 | 13 | trivial, smoke only |
| `deadlock_d2` | 1/2 | 11 | trivial |
| `atomicity_d2`, `pad = 0` | 1/2 | 11 | trivial |
| `atomicity_d2`, `pad = 1` | 3/8 | 13 | headline |

`pad` puts the same dummy reads before the snapshot on both twins, and placement matters. Reads between the snapshot and the store widen the race window, and the exact rate climbs to `1 - 2 ** -(pad + 1)` (3/4 at `pad = 1`). Reads before the snapshot make the two windows less likely to overlap: the rate is 1/2, 3/8, and 5/16 for `pad` 0, 1, and 2. `fit_atomicity_pad` returns the smallest pad under one half, which is 1. The lab does not add threads to make a bug rarer.

### First-failure table

`examples/first_failure_table.json` is the first-failure table for the headline subject (`atomicity_d2`, `pad = 1`, `k = 6`), with seeds `0` through `29`, `n_max = 2`, and cap 30:

| Policy | d | Schedules until first failure | Terminal | Schedule |
| --- | --- | --- | --- | --- |
| pct | 1 | 30 (no failure) | pass | depth 1 only runs one thread to completion |
| pct | 2 | 2 | fail | `[2, 2, 1, 1, 1, 2]` |
| pct | 3 | 2 | fail | `[2, 2, 1, 1, 1, 2]` |
| preemption | 1 | 4 | fail | `[1, 1, 2, 2, 2, 1]` |
| delay | 2 | 8 | fail | `[1, 1, 2, 2, 1, 2]` |
| random |  | 4 | fail | `[1, 1, 2, 2, 1, 2]` |
| dfs |  | 3 | fail | `[1, 1, 2, 2, 1, 2]` |
| fair |  | 1 | fail | `[1, 2, 1, 2, 1, 2]` |

`schedules_used` is the campaign count, summed over the bounds tried. For a bounded search, `seed` is the index inside the bound that produced the row, and `d` is that bound. Each row is one fixed-seed run on a six-step subject. The table shows that the harness works. It does not rank the policies.

## Run

From the repository root:

```text
python -m unittest discover -s projects/python-maintenance-regression-lab-case-003/tests -v
```

The same interpreter, with the project entry point:

```text
python projects/python-maintenance-regression-lab-case-003/run_lab.py budget --n 2 --k 6 --d 2
python projects/python-maintenance-regression-lab-case-003/run_lab.py replay projects/python-maintenance-regression-lab-case-003/examples/atomicity_d2_buggy.json --revision fixed
python projects/python-maintenance-regression-lab-case-003/run_lab.py replay projects/python-maintenance-regression-lab-case-003/examples/atomicity_d2_buggy.json --revision buggy
python projects/python-maintenance-regression-lab-case-003/run_lab.py campaign --subject atomicity_d2 --revision buggy --pad 1 --policy pct --d 2 --seeds 0-29 --n-max 2 --artifact-dir projects/python-maintenance-regression-lab-case-003/artifacts
python projects/python-maintenance-regression-lab-case-003/run_lab.py screen
python projects/python-maintenance-regression-lab-case-003/run_lab.py smoke
```

`campaign` exits 1 when it finds `fail` or `deadlock`, and 0 for `pass` or `cap`. `replay` exits 0 on `pass`, 1 on `fail` or `deadlock`, and 2 on `diverged`, `step-cap`, or `bound_exceeded`. Usage and artifact errors also exit 2. `replay` rebuilds the subject with the artifact's `pad`. `replay --artifact-dir DIR` also writes the replay as a new artifact with this interpreter's stamp. `smoke` exits 0 either way. There is no default log path. Without `--artifact-dir`, the campaign command writes under `artifacts/` inside this project, which the project `.gitignore` excludes. Each run's JSON and DEBUG log share a stem: `{subject}-{revision}-{policy}-d{d}-seed{seed}`, with `-pad{pad}` after the subject name when the subject is padded.

## Design decisions

The gate is the simulator. PCT and a controlled search both need the tester to choose the next thread, and a saved interleaving has to be replayable. Real OS threads do not give that.

Priorities and the controlled-random choices come from `random.Random(seed)`. The global `random` module is not seeded. Before a subject runs, the harness refuses a predicate or an audited callable that names `time`, `random`, `datetime`, or `os.urandom`. It also catches names imported from those modules (`from time import sleep`), plain helper functions reached through globals or closures, and nested lambdas. The refusal happens before any artifact is written. The check reads code objects, so it is a tripwire, not a sandbox: `getattr` with a computed name or `eval` gets past it.

`fail` and `deadlock` are subject failures. `pass`, `step-cap`, `bound_exceeded`, `diverged`, and `cap` are not. Reaching the schedule cap records terminal `cap` and `schedules_used` equal to the cap.

Every artifact the harness writes, including a replay written with `--artifact-dir`, stores `sys.version` and the raw GIL stamp: `sysconfig.get_config_var("Py_GIL_DISABLED")`, the `PYTHON_GIL` environment string or null, and `sys._is_gil_enabled()` when that attribute exists. If the attribute is missing, `gil_enabled` is null and `gil_probe` is `unavailable`. The lab does not branch on mode codes.

Logging follows a file-plus-console split. `schedlab.schedule` logs one DEBUG line per step (`step`, `tid`, enabled tids, reason). `schedlab.campaign` logs one INFO line per run (`policy`, `seed`, `terminal`, `steps`, `schedules_used`). Named loggers propagate to a DEBUG file handler and an INFO console handler installed for the campaign and removed afterward. The console threshold is what keeps step lines off the console. The file formatter adds a timestamp. Each run has its own file, so the next seed does not truncate the previous trace. CI replays the JSON.

The native smoke runs the fixed protocols under real locks for a few iterations and returns `informational: true` and `artifact_written: false`. It is not the regression gate.

## Limitations

The simulator sees only instrumented ops. It does not wrap CPython the way a binary scheduler wraps a concurrency API. It does not establish freedom from bugs past the searched preemption or delay bound. The PCT floor applies to one depth, one `n_max`, and one `k`. A depth-3 row does not inherit the depth-1 floor. The same schedule may be drawn twice; PCT is not an exhaustive enumeration.

Sleep fuzzing is out of the harness on purpose. Network and IPC subjects are out of scope. Asyncio and context variables are out of scope. Oracles read integers and the lock graph. They do not call `gc.collect` and do not assert that an object was freed. A use-after-free that depends on collector timing is a story, not a test in this lab.

A finished bound-`c` search can still miss a bug that needs `c + 1` preemptions. The one-half trivial cutoff is a classification taken from a benchmark study, not a theorem. The exact rate covers controlled random only; PCT and the bounded searches have their own odds. The exact-rate walk is cheap because the planted subjects are tiny. It does not scale to long op lists.
