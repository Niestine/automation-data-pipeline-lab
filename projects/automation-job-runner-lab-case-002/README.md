# Reliable Automation Job Runner Lab — Case 002

A local multi-process Python queue for shift-slip exports. Workers claim one job at a time from a SQLite database in WAL mode, heartbeat a short lease measured by the database clock, write one CSV per idempotency key, and complete only while they still hold the fence granted at claim time. A crashed worker delays only that job, and only until the stored deadline. A paused worker cannot finish the job after a second owner has claimed it.

The slips are synthetic. Nothing in this lab calls a network service.

This case is a lease queue with crash recovery. It is not the DAG catalog runner in the earlier job-runner lab: there is no pipeline graph, no JSON lease file, and no dry-run overlay of workspace buckets. The work item is one shift slip, and the failure mode is two processes touching the same export.

## Problem

A weekly export job is safe only if a second worker can take over after a crash without writing a second CSV, and only if a worker that wakes up late cannot mark the job done after someone else has taken it.

- two processes must not export the same slip
- a crash after the claim must not hold the job forever
- a crash after the CSV write must not write a second file
- a client retry of the same request must not insert a second job
- a worker that misses its deadline must stop, and the next owner must wait until that deadline
- an operator needs a dry-run that prints the CSV without taking a lease

## Architecture

```
submit (idempotency key + canonical payload fingerprint)
  -> one queued row, or a replay / conflict / reject
worker
  -> BEGIN IMMEDIATE
  -> one due row: queued, or leased and already past lease_until
  -> fence = fence + 1, owner, lease_until = database now + term, attempts += 1
  -> commit
  -> insert effect_intents (idempotency key) while the fence is still live
  -> write outbox/<sha256>.csv outside any database transaction
  -> one transaction: intent applied, job succeeded, completion event
```

Package:

- `src/shiftlease/store.py` — WAL queue, submit contract, claim, renew, release, sweep, quarantine
- `src/shiftlease/worker.py` — effect loop, heartbeat thread, process-local jeopardy, resume of this owner's live leases
- `src/shiftlease/effect.py` — exclusive CSV create; identical bytes are a replay
- `src/shiftlease/jitter.py` — full-jitter delays and the single-writer cohort model
- `src/shiftlease/pool.py` — subprocess entry points used by the multi-process and process-kill tests
- `src/shiftlease/cli.py` — `submit`, `slot`, `worker`, `status`, `sweep`, `purge`
- `examples/` — three synthetic slips and one weekly slot

Deadlines use `datetime('now')` inside SQL, plus an integer `clock_shift_seconds` stored in `meta` so tests can step the database clock. Worker wall clocks do not extend `lease_until`.

The claim transaction is the mutual exclusion. SQLite WAL allows one writer, so `BEGIN IMMEDIATE` stands in for PostgreSQL `FOR UPDATE SKIP LOCKED`: one eligible row, then commit, then the effect. A worker stuck in the CSV write does not block claim of a different job.

Every protected update repeats `id`, `owner`, `fence`, and `lease_until` still ahead of the database clock. The fence increases only in that claim statement.

## Run

Python 3.10 or newer. The standard library is enough. From the repository root:

```text
python -m unittest discover -s projects/automation-job-runner-lab-case-002/tests -v
```

Sample batch (a temporary directory unless you pass `--state-dir`):

```text
python projects/automation-job-runner-lab-case-002/run_lab.py
python projects/automation-job-runner-lab-case-002/run_lab.py --dry-run
python projects/automation-job-runner-lab-case-002/run_lab.py --state-dir <state-dir>
```

The live report has `"succeeded": 3`, `"applied_intents": 3`, and `"csv_files": 3`. `--dry-run` inserts the three sample slips, prints the first CSV, leaves every job `queued` with `attempts` 0, and writes no CSV file.

Module CLI, with `projects/automation-job-runner-lab-case-002/src` on `PYTHONPATH`:

```text
python -m shiftlease --help
python -m shiftlease --db <state-dir>/queue.sqlite submit --key <uuid> --payload-file <payload.json>
python -m shiftlease --db <state-dir>/queue.sqlite --outbox <state-dir>/outbox worker --max-jobs 1
python -m shiftlease --db <state-dir>/queue.sqlite status
python -m shiftlease --db <state-dir>/queue.sqlite slot --schedule-id weekly-lane --slot-start 2026-10-05T00:00:00Z --payload-file <payload.json>
python -m shiftlease --db <state-dir>/queue.sqlite sweep
python -m shiftlease --db <state-dir>/queue.sqlite purge --yes
```

`--help` states that the Idempotency-Key rules follow expired draft `draft-ietf-httpapi-idempotency-key-header-07` (work in progress, expired 18 April 2026), not an RFC. The fingerprint algorithm is `sha256-canonical-json-v1`: SHA-256 over UTF-8 JSON with sorted keys and compact separators. Terminal keys are retained for 7 days (`604800` seconds) after `finished_at`. A purged key can be used again only because the row is gone.

Exit codes: `0` created, replayed, dry-run, status, or a worker that finished the requested job; `2` missing key, malformed payload, or payload mismatch; `3` the worker observed the recovery quarantine and granted nothing; `4` the same key is already queued or leased; `1` the worker lost its fence (jeopardy) or did not drain, or the database could not be opened or stayed busy.

`worker` without `--max-jobs` drains: once nothing is queued or leased it confirms with 20 empty polls under full jitter (typically about 8 seconds and at most about 17 with the default 1-second cap) and exits. A queued job whose `run_at` is still in the future keeps it polling.

Copy or restore `queue.sqlite` together with `queue.sqlite-wal`, `queue.sqlite-shm`, and `queue.sqlite.leaseguard`.

Logging: worker events are JSON objects on stderr. Each line has `event`, `job_id`, `owner`, `fence`, and `seq`. Empty polls and busy retries also log `attempt` and `delay_seconds`.

## Design decisions

- **One shift slip is one job.** The payload is `desk` plus 1 to 50 rows of `sku`, `qty`, and `bin`. The effect writes a CSV whose first column is the idempotency key. A downstream lane tool is assumed to read that file. This process does not ship the tool.
- **Submit follows the draft's matrix.** First insert returns 201. The same key and fingerprint while the job is in flight returns 409 and the original id, and does not change `attempts`. The same key and fingerprint after success or dead-letter returns 200 and the stored result or error. A different fingerprint returns 422. A missing key returns 400.
- **The lease term is short, and the clock that matters is the database clock.** The CLI default is a 4-second term, a 2-second heartbeat, and a 1-second uncertainty allowance. `run_lab.py` uses a 30-second term and no heartbeat thread because the sample batch finishes immediately. Lease, fence, and dead-letter tests step `clock_shift_seconds` instead of sleeping; only the heartbeat-thread test uses the wall clock. A forward step makes a lease reclaimable. A backward step keeps the absolute deadline, so a second owner is still excluded.
- **The holder stops one uncertainty allowance early.** Other workers wait until the stored `lease_until`. That split is Gray's clock allowance applied to job ownership.
- **Recovery quarantine.** If `queue.sqlite.leaseguard` records a higher commit epoch than the opened database, or the guard file is corrupt, new grants wait until the database clock passes `recovery_quarantine_until` (now plus the maximum term recorded in the guard). Committed leases on a normal reopen are honored as they stand.
- **Fence on every write that changes ownership or records success.** Completion, renewal, release, the intent insert, and the applied mark fail closed when the row count is zero. The worker then sets process-local jeopardy and stops.
- **Effect intent before the file, completion after.** The CSV write is not in a database transaction. The file is created with `O_EXCL`. The same bytes on a later call are a replay. The attempt budget and the effect-retry budget are separate. Exhausting effect retries records `last_error` and lets the lease expire. `sweep` moves an expired row to `dead` exactly when `attempts >= max_attempts`, and later claims skip it. A row released back to `queued` on its last attempt is swept the same way instead of sitting unclaimable. A drain loop treats a dead job's leftover `pending` intent as finished, not as remaining work.
- **Early release** sets the deadline to the database now and the status back to `queued`. The attempt already counted stays counted. The next claim does not wait out the original term. This is a `QueueStore.release` call; the worker loop itself never releases, it either completes or lets the lease expire.
- **Full jitter.** Empty polls, SQLite busy errors, and effect retries sleep `random(0, min(cap, base * 2^attempt))`. Grant length adds `0..lease_jitter_seconds` whole seconds because `datetime` resolves to one second. The cohort comparison is a deterministic single-writer model, seed `20261005`, 20 clients, 20 jobs: 19 failed claims with full jitter, 57 with synchronized backoff, synchronized drain time 5.0 time units, jitter drain shorter. That model is not a measurement of this SQLite file and it is not Brooker's published simulation.
- **Per-job event log.** `(job_id, seq)` orders claim before the completion that carries the same fence. Cross-job order is only the order SQLite committed those transactions.
- **Schedule identity** is `sched:{schedule_id}:{slot_start}` with `slot_start` shaped `YYYY-MM-DDTHH:MM:SSZ`. The same slot inserts one row.

## Limitations

- WAL is one writer on one computer. This is not a Chubby cell, a ZooKeeper ensemble, or a multi-host queue. Do not put the database on a network filesystem.
- Fencing the SQLite row does not stop a peer that ignores the idempotency key. `CsvEffect` honors the key. A different writer can still create a second file.
- The Idempotency-Key document is an expired Internet-Draft, not an RFC.
- Brooker's "more than half the calls" result belongs to his simulator (100 clients, 10 ms mean delay). This lab records 19 versus 57 in its own model.
- Gray's paper is about file-cache leases. The lab uses the time bound, the clock allowance, and the honor-or-quarantine recovery rule. It does not implement cache invalidation.
- SQS can hand the same message to two consumers even inside the visibility window. A holder that keeps working after expiry without the fence check can overlap the next owner here too.
- `datetime('now')` is one-second resolution. Lease jitter is a whole number of seconds.
- Quarantine detects a leaseguard epoch ahead of the recovered database, and a corrupt guard. A commit that never updated the guard is not distinguishable from a commit that did not happen. The guard is rewritten after each commit, so two processes that commit back to back can leave it one epoch behind; that narrows detection and never causes a false quarantine.
- A normal close may checkpoint the WAL into the main file. The durability test that refuses grants copies the main file while a connection is still open and `wal_autocheckpoint` is 0, so the claim frames are not in that copy. Deleting `-wal` and `-shm` after a checkpoint that already folded them in does not look like data loss, because the committed lease is in the main file.
- Process-death tests terminate the worker with the operating system's process kill after the claim has either committed or been left uncommitted. `Popen.kill()` is `TerminateProcess` on Windows and `SIGKILL` on POSIX; the suite was run on Windows.
- The heartbeat thread is real. `tests/test_heartbeat.py` runs it on the wall clock (3-second term, 1-second heartbeat, 3.5-second effect): with the thread the job succeeds under fence 1, without it the completion is rejected. Other tests call renewal directly so they do not sleep. Heartbeats are per claim: an effect that gives up its lease does not stop renewals for the next job. A CSV write already inside `perform` is not cancelled mid-call; jeopardy blocks the next call and the completion.
- `status` is a short read. A read that stayed open would pin a WAL checkpoint. This lab closes the read transaction before returning.
- Dry-run renders the next due CSV and writes nothing. It does not simulate a crash.
- This is a portfolio sample with synthetic slips, not a production scheduler.

## What it demonstrates

- A WAL queue whose claim, renew, release, and completion are conditional on a fence granted in the claim statement
- Database-clock leases, an uncertainty allowance, early release, and a quarantine when the leaseguard is ahead of the recovered file
- Crash cuts around the CSV: uncommitted rollback, kill after commit, kill before commit, and one file per idempotency key across intent / effect / applied gaps
- Submit replay, in-flight conflict, payload mismatch, missing key, retention purge, and a schedule slot that cannot be inserted twice
- Dead-letter at `max_attempts` with `last_error` preserved, and no dead-letter while a renewal is still succeeding
- A second job claimed while the first effect is still running, and eight processes finishing 100 jobs with one applied intent each
- A real heartbeat thread that carries a slow effect past its original term
- Full-jitter delays versus a synchronized cohort in a deterministic model, structured JSON logs, and a dry-run that does not take a lease
