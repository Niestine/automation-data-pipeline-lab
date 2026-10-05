# Python Maintenance & Regression Lab — Case 007

A cold-store bay desk posts one non-idempotent hold notice to a scripted gate. The historical client retried too soon, retried forever, double-charged the bay, and multiplied load down a three-tier chain. This lab keeps those builds and locks the repair with fault injection, a virtual clock, and regression oracles.

The suite is offline. It does not open a socket, read a wall clock, or call a live gate.

## Problem

A hold notice charges a dock fee exactly once. The gate sometimes times out, sometimes rejects the bay, and sometimes answers `OVERLOADED`. The old desk treated those the same way: a tight loop, a missing cap, a buffer that kept the failed attempt's partial text, and a late response from attempt 1 that was stored as the round-trip time of attempt 2 and charged again.

The repaired desk separates three decisions:

- whether this error type is retried
- how many additional attempts are allowed, and how long the virtual clock waits
- how attempt state, the charge ledger, and the RTT sample are reset

## Architecture

`src/bay_notice/` is one in-process service.

| Module | Role |
| --- | --- |
| `client.py` | Loop desk and re-queue desk. Both stamp a token, reset the attempt buffer, and emit the same log fields. |
| `policy.py` | Retryable type set, cap, floor, multiplier, ceiling, and jitter. |
| `timer.py` | SRTT, RTTVAR, and RTO. The TCP-shaped preset follows RFC 6298. |
| `budget.py` | One process-wide retry budget shared by every tier. |
| `inject.py` | Scripted gate. An empty schedule is the success path. Installed steps force the error. |
| `ledger.py` | Charge ledger keyed by operation id. |
| `log.py` | One `LogRecord` per transmission. |
| `handlers.py` | Repaired predicate, plus the empty, swallow, rewrite, and broad-abort fixtures. |
| `amplify.py` | Retry amplification under a constant failure probability. |
| `trace.py` | Frozen correlated-failure schedule and the three policies. |
| `capacity.py` | In-process overload cliff. |
| `clock.py` | Virtual clock. `sleep` records the delay and advances `now`. |

A notice id looks like `BAY-1001`, a bay like `C-14`, and a charge like `18.00`. Invalid notices raise `JobRejected` before the gate is called.

`max_retries` counts additional attempts. An always-fail schedule stops at `max_retries + 1` transmissions. A broken cap is allowed one extra transmission so the oracle can see it, then a safety stop ends the run.

Named builds in `client.BUILDS` are the defects: `no-backoff`, `ignore-cap`, `accept-stale`, `concat-partial`, `retry-permanent`, `skip-transient`, `empty-handler`, `swallow-handler`, `rewrite-handler`, `broad-abort`, `requeue-drops-cap`, `requeue-drops-delay`. `repaired` is the maintenance fix.

## Run

From the repository root:

```text
python projects/python-maintenance-regression-lab-case-007/run_lab.py run projects/python-maintenance-regression-lab-case-007/examples/gate_schedule.json
python projects/python-maintenance-regression-lab-case-007/run_lab.py run projects/python-maintenance-regression-lab-case-007/examples/gate_schedule.json --log
python projects/python-maintenance-regression-lab-case-007/run_lab.py timer
python projects/python-maintenance-regression-lab-case-007/run_lab.py trace projects/python-maintenance-regression-lab-case-007/examples/correlated_trace.json
python -m unittest discover -s projects/python-maintenance-regression-lab-case-007/tests -v
```

`run` posts the synthetic hold in `examples/gate_schedule.json`. The gate returns one transient error that leaves a partial `HOLD-`, then accepts the notice. The repaired desk waits `0.25` seconds on the virtual clock, commits `18.00` once, and returns the hold body without the partial text. `--log` also writes one JSON line per attempt record to stderr, built from the record attributes. `--build NAME` runs a historical build instead of the repair. A rejected notice or a malformed fixture prints a JSON error to stderr and exits 2. `timer` prints the no-sample RTO table for the TCP-shaped preset: it starts at 1 second and clamps at 60. `trace` scores `examples/correlated_trace.json`.

Python 3.10+ and the standard library are enough. No third-party packages. The suite has been run on CPython 3.10 and 3.13.

## Design decisions

The gate is a list of steps, not a thread and not a socket. Tests pop `timeout`, `error`, `success`, `late`, or `overloaded`. The same success-path call is the injection point: with no steps installed the desk transmits once; with a transient step installed it transmits twice.

Backoff arms the current RTO, waits that long on the virtual clock, then doubles: `min(ceiling, max(floor, RTO * multiplier))`. With the jitter sample fixed at 1, floor 1, and ceiling 6, the waits are `1, 2, 4, 6`. A zero wait fails the delay oracle. The cap oracle fails when transmissions continue past `max_retries + 1`.

The timer profile `tcp_6298` uses a 1 second floor, K = 4, alpha 1/8, beta 1/4, granularity G, and a ceiling of at least 60 seconds. Before any sample, RTO is the floor and still doubles on timeout. The first sample R sets `SRTT = R`, `RTTVAR = R/2`, `RTO = SRTT + max(G, 4*RTTVAR)`. Later samples update RTTVAR from the previous SRTT, then update SRTT. A computed RTO below the floor is stored as the floor. A zero variance term uses G. After repeated doublings the estimate may be cleared so the next unambiguous sample is treated as a first sample. The application preset uses a 0.05 second floor. That preset is more aggressive than RFC 6298 allows for TCP, and the tests keep the two profiles separate. The SYN-to-3-second handshake rule is not part of this desk.

Every attempt gets a token `{operation_id}.{attempt}` before the send. An RTT sample is taken only when the response token matches the outstanding attempt. A late success for attempt 1, delivered while attempt 2 is outstanding, leaves SRTT and RTTVAR unchanged and does not commit. The charge ledger is keyed by operation id, so a second delivery does not append or charge again. A late delivery after the desk gave up on a notice is not committed either: a failed hold is never charged. The broken `accept-stale` build keys by token and does both. Per-attempt buffers reset on send, so a partial `HOLD-` from a failed attempt is absent from the successful body. The broken `concat-partial` build keeps it.

The retryable set is an explicit set of classes. `TransientGateError` is retried. `PermanentGateError` propagates as itself. A subclass that is not in the set is not retried. `OVERLOADED` updates the shared budget and returns without another immediate send. `BudgetExhausted` is raised once and is not retried at this tier.

The shared budget follows the preprint's adaptive procedure: failure rate is an exponential moving average (`0.9 * old + 0.1 * outcome`), the budget starts at 0.2 of base load, a retry is admitted with probability `min(budget, 1 - failure_rate)`, high failure or `OVERLOADED` multiplies the budget by 0.5, and a low failure rate relaxes it by 0.1 up to the initial budget. These are the constants printed in the preprint's Algorithm 1. Admission uses a seeded or replayed RNG. The desk reports both failures and successes to the budget, so the failure rate can recover. It checks the cap before asking the budget, so a retry the cap already forbids never spends shared allowance.

Jitter multiplies the armed delay by a factor, then clamps to the floor when the floor is positive. The fixed sampler returns 1, so every desk records the same waits; the desynchronization check fails that build. The seeded sampler draws a factor from `1 ± spread`. Independent desks do not share a timestamp sequence, and a factor below 1 cannot pull a positive floor down.

The re-queue desk places the next attempt on a `deque`. It uses the same cap, delay, token, reset, and log rules as the loop. `requeue-drops-cap` and `requeue-drops-delay` are separate failing builds.

Each transmission logs `operation_id`, `attempt`, `token`, `error_type`, `decision` (`retry`, `stop`, `budget_refuse`, `not_retryable`), `delay_s`, and `rto_s` as `LogRecord` attributes. A second template drops the word "decision" from the sentence; the attribute check still passes. The decision oracle `assert_every_send_decided` fails when a transmission ended with no decision. The empty and log-only handler builds fail it, and the repair passes. The library logger only gets a `NullHandler`. Level and destination are the caller's choice.

Retry amplification for one tier at constant failure probability `p` and `n` additional retries is `(1 - p^(n+1)) / (1 - p)`. The published point `p = 0.5`, `n = 3` is 1.875. Enumerating all 16 equally likely fair-coin sequences through the same attempt counter gives that ratio exactly, and the enumeration matches the closed form for `n = 0..5`. A seeded run of 20,000 jobs stays within 0.03. Three tiers that each apply the policy stay within 0.2 of the cube: about 6.59 at `p = 0.5` and about 2.85 at `p = 0.3` (8,000 jobs, seed 7). A zero-retry run on the same jobs reports RAF 1. Four always-fail attempts at three uncoordinated tiers issue 64 gate calls. The same tier walk with one shared budget, on `OVERLOADED`, issues 1, because every tier's retry admission is refused.

The correlated fixture is `examples/correlated_trace.json`. While a job's fault flag is set, a retry dooms the next job. On that schedule the locked counts are:

| Policy | Successes | Calls | Success rate | RAF |
| --- | --- | --- | --- | --- |
| no-retry | 5 / 8 | 8 | 0.625 | 1 |
| standard (3 attempts) | 3 / 8 | 19 | 0.375 | 2.375 |
| budgeted | 6 / 8 | 9 | 0.75 | 1.125 |

Standard success is below no-retry. Budgeted success is at least no-retry. Budgeted RAF is below standard RAF. These are this fixture's counts. They echo the preprint's direction (uncoordinated retry finished worse than not retrying; a budget held the line and spent less load) and they are not that paper's 41.5% and 55.4%.

The capacity model uses the worked example of 10,000 QPS healthy, collapse at 11,000, still crashing at 9,000, and recovery near 1,000 when about a tenth of the instances can serve. A balancer that removes an instance after one injected error has a lower highest stable load than one that keeps using that instance (9,899 versus 10,999 on a 10,000 capacity with 10 of 100 instances removed). Offering `1.2 * 10,000` collapses a fleet that still serves the unretried 10,000.

Maintenance workflow: name the defect, select its build, inject the schedule the success path already calls, and assert the oracle. The broken build raises `OracleFailure` or the original bad outcome. The repaired build passes the same oracle.

## Limitations

The gate schedule is deterministic and in-process. It does not measure a network, a queue, or a real cold-store controller.

The closed-form RAF assumes a constant independent failure probability. It is the unit-test metric for a policy that would otherwise assert only eventual success. It is not an outage SLO. Backoff can make a realized factor smaller; overload can make it larger.

The correlated trace is one frozen doom-next schedule with a published list of admission draws. The Bernoulli admission coin is tested on its own. The trace comparison admits when that published draw is below the probability and refuses when the budget is exhausted or the gate is `OVERLOADED`.

The Java incident shares and the Python configuration audit describe other repositories. They are not frequencies for this desk. The capacity ratios are one worked example, not a fitted curve. The model has a sharp cliff and omits cache-hit rate, queues, and process start time.

Jitter here is a seeded spread around 1 with the floor applied after the sample. It is the desynchronization check from the amplification study. It is not a named full-jitter or equal-jitter formula.

HTTP `Retry-After`, gRPC hedging, and a mandated call to `time.monotonic` are outside this desk. Deadlines and backoff use the virtual clock only.

## What this demonstrates

- Fault injection on the call the success path already makes, including a path with no schedule installed.
- A cap oracle and a delay oracle on both the loop and the re-queue desk.
- An explicit retry predicate, permanent errors that keep their type, and handler fixtures for an empty handler, a log-only swallow, a rewritten exception type, a broad abort, and `FIXME` / `TODO` markers.
- RFC 6298's sample order, floor, granularity, doubling, ceiling, and Karn's rule for a late token.
- An operation-id ledger that charges a hold once, and a buffer reset that drops partial attempt text.
- Structured log attributes that survive a wording change.
- Single-tier and three-tier amplification checks, a shared adaptive budget, and the locked no-retry / standard / budgeted comparison.
- An overload cliff and an error-avoiding balancer, using the published 10,000 / 11,000 / 9,000 / 1,000 example.
