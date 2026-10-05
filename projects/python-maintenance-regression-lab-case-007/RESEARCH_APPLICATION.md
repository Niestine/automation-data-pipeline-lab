# Research application

Case 007 is a bay-hold desk. One notice charges a dock fee through a scripted gate. The repairs and the tests come from five public sources. The suite runs offline on synthetic fixtures.

## Techniques used

### 1. Inject the retry at the call the success path already makes

Sources:

- Bogdan Alexandru Stoica, Utsav Sethi, Yiming Su, Cyrus Zhou, Shan Lu, Jonathan Mace, Madanlal Musuvathi, and Suman Nath, *If At First You Don't Succeed, Try, Try, Again...? Insights and LLM-informed Tooling for Detecting Retry Bugs in Software Systems* (SOSP 2024). <https://www.microsoft.com/en-us/research/publication/if-at-first-you-dont-succeed-try-try-again-insights-and-llm-informed-tooling-for-detecting-retry-bugs-in-software-systems/>
- Ding Yuan, Yu Luo, Xin Zhuang, Guilherme Renna Rodrigues, Xu Zhao, Yongle Zhang, Pranay U. Jain, and Michael Stumm, *Simple Testing Can Prevent Most Critical Failures* (OSDI 2014). <https://www.usenix.org/conference/osdi14/technical-sessions/presentation/yuan>

`inject.Gateway` starts empty. `tests/test_injection.py` posts `BAY-1001` and records one transmission. The same `Desk.run` call, with a transient step pushed in front of success, records two transmissions and returns the hold body. The study's missing-cap and missing-delay oracles are `policy.assert_cap` and `policy.assert_positive_delays`. `max_retries` is the number of additional attempts, so the ceiling is `max_retries + 1`. The `ignore-cap` build transmits `max_retries + 2` times and the cap oracle raises. The `no-backoff` build records zero virtual-clock gaps and the delay oracle raises. The repaired build on an always-fail schedule stops at exactly four transmissions when `max_retries` is 3, and the waits for floor 1 and ceiling 6 are `1, 2, 4, 6`.

The same oracles run against `run_requeue`, which puts the next attempt on a `deque`. About 45% of the mechanisms in the SOSP study were not loops. `requeue-drops-cap` and `requeue-drops-delay` fail the oracles the loop desk passes.

The predicate, the cap, and the state reset are separate, matching the study's split into whether to retry, when, and how. `PermanentGateError` is raised once, as itself. `skip-transient` does not retry `TransientGateError`. A partial `HOLD-` left by attempt 1 is absent from the repaired success body and present in the `concat-partial` body.

### 2. Non-fatal handlers and structured log fields

Source: Yuan, Luo, Zhuang, Rodrigues, Zhao, Zhang, Jain, and Stumm, OSDI 2014. <https://www.usenix.org/conference/osdi14/technical-sessions/presentation/yuan>

The paper's testing method is to force a non-fatal error on an otherwise normal run, and to treat an empty handler, a handler that only logs, an over-broad catch, and a `FIXME` or `TODO` left in the handler as separate defects. `handlers.py` keeps those four broken shapes. `tests/test_handlers.py` injects one `TransientGateError`. The empty handler returns success with no decision and no log record. The swallow handler logs a sentence and the `LogRecord` has no `decision` attribute. The rewrite handler raises `WrappedHandlerError`. The broad abort raises `OperationAborted` whose cause is the original transient. The repair retries that transient once and then returns the hold. `policy.assert_every_send_decided` is the oracle for the two silent shapes: it fails when a transmission ended without a logged decision, so the empty and swallow builds raise `OracleFailure` and the repair passes. `handler_markers` reads the function source: `empty_handler` contains `FIXME`, `swallow_handler` contains `TODO`, and `repair_handler` contains neither.

Log records carry `operation_id`, `attempt`, `token`, `error_type`, `decision`, `delay_s`, and `rto_s`. `tests/test_logs.py` reads those attributes. The default sentence contains `decision retry`. The template `note {operation_id}/{attempt}` does not, and the attribute check still passes. That is why the oracle does not search the formatted sentence. The OSDI study found the triggering events were usually already in the log, and that matching the prose is brittle.

### 3. RTO floor, update order, and Karn's token

Source: V. Paxson, M. Allman, J. Chu, and M. Sargent, *RFC 6298: Computing TCP's Retransmission Timer*. <https://www.rfc-editor.org/rfc/rfc6298>

`timer.RtoEstimator` copies the computation, with the constants configurable. The `tcp_6298` preset uses a 1 second floor and a ceiling of 60 seconds. Until a sample exists, RTO is the floor and doubles on each timeout, clamped at the ceiling. The first sample R sets `SRTT = R` and `RTTVAR = R/2`. The next sample updates RTTVAR from the previous SRTT with beta 1/4, then updates SRTT with alpha 1/8. `tests/test_timer.py` uses samples 1 then 3: RTTVAR is 0.875. Updating SRTT first would store 0.8125, and the test asserts those differ. A computed RTO below the floor is stored as the floor. A zero variance term yields `RTO = SRTT + G`. After two backed-off timeouts with `reset_after=2`, SRTT and RTTVAR are cleared and the next sample is a first sample (`RTTVAR = R/2`).

Karn's rule is the token check in `client.py`. A response is an RTT sample only when its token equals the outstanding attempt token. `tests/test_karn.py` times out attempt `BAY-1001.0`, starts `BAY-1001.1`, and delivers a success carrying the first token. The event records the outstanding token, zero commits, and a null SRTT. The matching token then commits once and sets SRTT to that sample. A second delivery does not change the commit count or SRTT. A late delivery after the desk gave up commits nothing. The broken `accept-stale` build samples the late RTT and charges the bay twice. The ledger key on the repair is the operation id.

The application preset's floor is 0.05 seconds. The tests assert it is below the TCP floor. This desk is not a TCP stack, and the SYN re-initialization to 3 seconds is not implemented.

### 4. Retry amplification, the shared budget, and desynchronized waits

Source: Ishaan Mehan and Arjun Saluja, *Retry Amplification in Distributed Systems: A Systematic Analysis of Retry Policies and Their Role in Cascading Failures*. <https://arxiv.org/abs/2608.25403>

`amplify.expected_raf` is `(1 - p^(n+1)) / (1 - p)`. `tests/test_raf.py` locks `p = 0.5`, `n = 3` at 1.875, including an exact half-coin table (30 calls over 16 jobs). `exact_half_load` derives it by enumerating every fair-coin sequence through `attempt_count`, and the test checks the enumeration against the closed form for `n = 0..5`. A seeded run of 20,000 jobs stays within 0.03. The three-tier bound is that factor cubed: 6.591796875 at `p = 0.5` and about 2.85 at `p = 0.3`. Seeded runs of 8,000 jobs stay within 0.2 of those bounds. Zero retries report RAF 1. Four always-fail attempts at three uncoordinated tiers count 64 terminal calls in `tier_calls`. `shared_overloaded_calls` runs the same `tier_calls` walk with one `SharedBudget`, reports each failure as `OVERLOADED`, and stops at 1 call because `try_admit` refuses at all three tiers (the test checks 3 refusals and 0 admissions).

`budget.py` implements the preprint's Algorithm 1. The failure rate is `0.9 * previous + 0.1 * outcome`. The budget starts at 0.2. Admission probability is `min(budget, 1 - failure_rate)`. A high failure rate or `OVERLOADED` multiplies the budget by 0.5. Below the low threshold the budget steps back by 0.1, capped at the initial value. An exhausted or overloaded admission raises `BudgetExhausted` or `OverloadedSignal` from the desk and does not transmit again. The desk checks its cap before asking the budget, and reports successes as well as failures, so the failure rate recovers from desk traffic. `tests/test_budget.py` checks a draw of 0.19 against probability 0.2 (admit) and a draw of 0.2 (refuse), a strictly decreasing admission probability across repeated `OVERLOADED` outcomes, and recovery after successes bring the failure rate back under 0.05.

The correlated schedule is `examples/correlated_trace.json`, written as the job list and the admission draws. A retry while the fault flag is set dooms the next job. `tests/test_trace.py` locks the lab's own counts: no-retry 5 successes and 8 calls, standard 3 and 19, budgeted 6 and 9. Standard success is lower, budgeted success is at least the no-retry line, and budgeted RAF (1.125) is below standard RAF (2.375). The preprint's 41.5% and 55.4% are the direction of that comparison. They are not a target this process has to hit.

The same paper is why a fixed delay sample is a failing build. Backoff without jitter puts independent clients on the same timestamps. `tests/test_jitter.py` runs four desks. With the sampler fixed at 1, every desk records `1, 2, 4, 6` and `assert_desynchronized` raises. With a seeded spread, the timestamp sequences differ, and no wait falls below the floor. A factor of 0.1 against floor 1 stays at 1. A floor of 0 is allowed to land below the pre-jitter base. This is not the full-jitter formula from a different source; the floor is applied after the sample so the delay oracle still has a gap.

### 5. Overload recovery at the residual healthy capacity

Source: Google, *Site Reliability Engineering*, Chapter 22, *Addressing Cascading Failures*. <https://sre.google/sre-book/addressing-cascading-failures/>

`capacity.CapacityModel` uses the chapter's worked ratios. Capacity 10,000 stays stable at 10,000, collapses at 11,000, stays in the crash loop at 9,000, and recovers at 1,000, with the healthy fraction set to 0.1 while the loop flag is set. Retries count as offered load: base 10,000 with RAF 1 stays stable, and the same base with RAF 1.2 (12,000) collapses. A balancer that removes 10 of 100 instances after an injected error has its highest stable load at 9,899, below the 10,999 of a balancer that keeps those instances. The chapter presents these ratios as one example of a cliff, and the tests treat them that way.

## What was deliberately left out

No HTTP `Retry-After` parser, no method-idempotency table, no gRPC hedge or token-bucket throttle, and no named full-jitter equation. The virtual clock is not `time.monotonic`. The TCP preset does not re-initialize a sub-3-second SYN timeout. The amplification runs are expectations and one frozen trace, not a claim about production traffic.
