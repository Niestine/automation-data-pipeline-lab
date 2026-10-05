# Research application

Five public sources shape this lab. Each technique below is implemented under `src/schedlab` and locked by `tests/`. The subjects are synthetic. No hit rate from the papers is reused as a result of this project. The numbers in `README.md` and `examples/first_failure_table.json` are from this interpreter.

## Controlled steps, then a recorded schedule

Sources:

- Sebastian Burckhardt, Pravesh Kothari, Madanlal Musuvathi, and Santosh Nagarakatte, "A Randomized Scheduler with Probabilistic Guarantees of Finding Bugs", ASPLOS 2010. https://www.microsoft.com/en-us/research/publication/a-randomized-scheduler-with-probabilistic-guarantees-of-finding-bugs/
- Madanlal Musuvathi, Shaz Qadeer, Thomas Ball, Gerard Basler, Piramanayagam Arumuga Nainar, and Iulian Neamtiu, "Finding and Reproducing Heisenbugs in Concurrent Programs", OSDI 2008. https://www.usenix.org/conference/osdi-08/finding-and-reproducing-heisenbugs-concurrent-programs

`machine.execute` runs one op of one enabled logical thread and counts that as one step. The shared state is integer cells and locks. `policies.run_pct`, `run_random`, `run_fair`, and `search_schedules` are the only places that choose a tid. A subject whose predicate names `time`, `random`, `datetime`, or `os.urandom` raises `SoundnessError` before a file is written. So does one that reaches `time.sleep` through `from time import sleep` and a helper function, or through a nested lambda (`tests/test_campaign.py`). The check is a tripwire over code objects, not a sandbox. The interpreter's re-entry counter peaks at 1. Two replays of `examples/atomicity_d2_buggy.json` produce the same terminal state. A plain list update outside `execute` does not advance the step counter.

PCT is the stress campaign. At creation each thread gets a distinct key from `random.Random(seed)`. Each step runs the highest-priority enabled thread. For depth `d` the harness draws `d - 1` change points from `1..k` before the run and, after a change-point step, sets that thread's key strictly below the others. Depth 1 draws none. `tests/test_scheduler.py` locks a fixed seed with `n = 3`, `k = 10`, `d = 3`: two distinct change points inside `1..k`, distinct initial keys, and the same tid repeated until a change point. `campaign.per_run_lower_bound` is `1 / (n * k ** (d - 1))`. `minimum_runs` is the smallest `R` with `(1 - p) ** R <= 0.01`. Depth 1 and depth 3 are different rows; the depth-1 floor is not quoted on a depth-3 run.

The regression gate is the tid list. `replay.replay` requires each recorded tid to be enabled, then runs one op. `examples/ordering_d1_buggy.json` (`[2, 2, 1, 1]`) is `fail` on the buggy user and `pass` on the fixed user. `examples/atomicity_d2_buggy.json` (`[1, 2, 1, 2]`) ends at `x == 1` on the buggy twin and `x == 2` on the fixed twin. A schedule whose third tid is not enabled is `diverged`, and the data-race gate rejects that. Both depth-1 priority orders of the ordering subject are produced by PCT itself; the failing order is one of them. Depth-1 PCT on the atomicity subject only runs one thread to completion and does not find the bug. Depth 2 does. That split is `examples/first_failure_table.json`.

## Deadlock is an empty enabled set, not a starved thread

Source: Musuvathi et al., OSDI 2008, same paper as above.

`machine._classify` reports `deadlock` only when no thread is enabled and at least one thread is still blocked. A PCT run that never dispatches an enabled thread ends `step-cap`. Fair scheduling (`run_fair`) picks the oldest `last_step` so a liveness run does not rename starvation as deadlock. On a two-thread lock-free subject whose `k` covers both op lists, fair finishes both threads.

The deadlock subject is the two-lock shape. Preemption bound 0 does not deadlock the buggy twin. Bound 1 does, on `[1, 2, 2, 1]`, and the preemption counter is 1: the later switch happens after a thread has blocked and costs 0. Delay bound 0 is the lowest-tid baseline and does not deadlock. Delay bound 1 does, with delay count 1, on a different schedule. The fixed twin takes the locks in one order. Bounds 0 through 5 report no deadlock. The coverage string is `no failure within bound c`. Replaying the buggy schedule on the fixed twin returns `diverged`. The deadlock gate accepts that only when the bounded search is clean, and it rejects a replay that still deadlocks.

A preemption costs 1 only when the previous thread is still enabled. Iterative search tries bound 0, then 1, through 5, and stops at the first subject failure or the schedule cap. `examples/first_failure_table.json` records schedules-until-first-failure for DFS, preemption bounding, delay bounding, controlled random, and PCT. PCT rows are split by `d`. `step-cap` and `cap` are not subject failures. Controlled random stays in the harness because every choice is recorded. Sleep fuzzing is not.

The trivial-bug rule comes from Thomson, Donaldson, and Betts, "Concurrency Testing Using Controlled Schedulers: An Empirical Study", ACM TOPC 2016 (https://dl.acm.org/doi/10.1145/2858651). That study treats a bug found on at least half of random schedules as trivial and keeps it only as a minimum baseline. The same study is the source of the five-policy comparison set and the bound-`c` coverage wording above.

`campaign.exact_failure_rate` walks every choice controlled random could make, weights each by `1 / len(enabled)`, and returns the exact failure probability as a fraction. `trivial_screen` tags a subject trivial when that rate is at least one half. It also runs the fixed seeds 0 through 29 (`examples/screen_seeds.json`, equal to `campaign.SCREEN_SEEDS`) and reports the sampled hits beside the exact rate. Ordering and deadlock are exactly 1/2, so both are trivial. Ordering stays a smoke test. Unpadded atomicity is also 1/2. `atomicity_d2(..., pad=n)` places the same dummy reads before the snapshot on both twins. One read brings the exact rate to 3/8, so the headline is `atomicity_d2` at `pad = 1`, and the first-failure table uses that subject. `tests/test_campaign.py` locks the rates 1/2, 3/8, and 5/16 for pads 0 to 2, checks them against a 2000-seed sample of `run_random`, and checks that the twins keep identical op kinds. The screen does not add threads.

Two choices here differ from a literal reading of the plan this lab was built from. First, the tag comes from the exact rate rather than from 30 sampled seeds. With every unpadded bug at exactly one half, a 30-seed count lands on either side of the cutoff depending on which seeds are picked. Second, the padding sits before the snapshot. Padding between the snapshot and the store widens the race window and pushes the rate up to `1 - 2 ** -(pad + 1)`, the opposite of the intent.

## The GIL stamp is not the lock

Source: PEP 703, "Making the Global Interpreter Lock Optional in CPython". https://peps.python.org/pep-0703/

`environment.probe` reads `sysconfig.get_config_var("Py_GIL_DISABLED")`, `PYTHON_GIL`, and `sys._is_gil_enabled()` when the attribute exists. The first call is cached for the process. Every campaign artifact, and every replay written with `replay --artifact-dir`, copies that stamp and `sys.version`. The tests compare the written file with a fresh read. The three hand-written fixtures under `examples/` carry the placeholder `fixture` instead of a stamp, and the README says so. Mode values are stored raw and are not interpreted. Subject state lives in integer cells touched only by ops. The suite does not call `gc.collect` and does not assert that an object was collected. `native.native_smoke` runs the fixed protocols on OS threads, sets `informational` and `artifact_written: false`, and is excluded from the replay gate.

## The log is a view of the artifact

Source: Python Software Foundation, "Logging Cookbook". https://docs.python.org/3/howto/logging-cookbook.html

`logsetup.CampaignLogs` installs a DEBUG file handler and an INFO console `StreamHandler` on the process root for the length of the campaign, then removes them. Formatters include the logger name, the level, and the message. The file formatter also includes `asctime`. `schedlab.schedule` emits one DEBUG line per step. `schedlab.campaign` emits one INFO line per run. Propagation plus the console threshold keeps `tid=` lines in the file and off the console. The artifact directory is the caller's. Each run opens its own file, the same stem as the JSON, so a later seed does not truncate the earlier trace. There is no default log path. CI replays the JSON. A missing log line is a logging bug, not a schedule bug.

## Limits kept in view

The probability statement is a lower bound for one depth on one run that stays inside `n_max` and `k`. Cooperative yields do not see races in code that never yields. A finished bound-`c` search does not prove the subject correct above `c`. This write-up does not depend on context-variable propagation, the free-threading howto, or asyncio debug mode.
