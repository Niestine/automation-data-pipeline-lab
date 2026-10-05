# Research application

Ten public sources shape this lab. Each technique below is implemented in `src/shiftlease` and locked by `tests/`. The shift slips are synthetic. The cohort counts are from this program's single-writer model (`tests/test_jitter.py`, seed `20261005`, 20 clients, 20 jobs): full jitter records 19 failed claims and synchronized exponential backoff records 57, with drain times of under 1 time unit and exactly 5.0. Those are not Marc Brooker's simulator results.

## Database-clock lease, honor the deadline, quarantine when the record is uncertain

Sources:

- Cary Gray and David Cheriton, "Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency", ACM SOSP 1989. https://dl.acm.org/doi/10.1145/74850.74870
- SQLite, "Write-Ahead Logging". https://www.sqlite.org/wal.html
- Amazon Web Services, "Amazon SQS visibility timeout". https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html

A lease is a time-bounded contract. `lease_until` is written with `datetime('now')` inside the claim transaction, plus the grant length. A worker wall clock never extends it. The default grant is 4 seconds, which is two heartbeat intervals; the holder renews at half the term. Gray's model subtracts an explicit clock-uncertainty allowance before the holder trusts the term. `holder_trusts` requires the stored deadline to be ahead of the database clock by `clock_uncertainty_seconds`. Other workers still wait until the stored deadline. `tests/test_lease.py` steps the database clock forward by 25 seconds on a 30-second grant with a 10-second allowance: the holder stops, and a second claim is still empty.

On a normal reopen the committed owner, fence, and deadline are honored (`tests/test_durability.py`). If the leaseguard epoch is ahead of the epoch recovered from the database file, which is what a copy of the main file without its uncheckpointed `-wal` frames looks like, the runner sets `recovery_quarantine_until` to now plus the recorded maximum term and grants nothing until that database time. A corrupt guard takes the same path. Gray's cache callbacks are not implemented. The allowance and the quarantine are the parts used here.

SQLite WAL is the local substrate: `journal_mode=WAL`, `synchronous=FULL` in the default configuration, and a `busy_timeout`. A commit is the commit record in the WAL. A claim that is rolled back before commit is invisible to a second connection, and killing the process in that window leaves the row `queued` with fence 0. Killing the process after commit leaves that one owner, fence, and deadline. Readers do not need the write lock: `status` runs a short deferred read while another connection holds `BEGIN IMMEDIATE`. The database file has to stay with its `-wal` and `-shm` companions. This is one writer on one host. It is not a multi-host log.

SQS is the operational picture of the same deadline. A worker that does not finish or extend the lease makes the job claimable again. A short term overlaps workers if they ignore the fence. A long term delays reclaim. The heartbeat exists so a live worker extends the deadline instead of burning an attempt. The 12-hour SQS cap is not copied.

## Fence from the grant, checked on every protected write

Sources:

- Martin Kleppmann, "How to do distributed locking", 2016. https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
- Mike Burrows, "The Chubby Lock Service for Loosely-Coupled Distributed Systems", OSDI 2006. https://www.usenix.org/conference/osdi-06/chubby-lock-service-loosely-coupled-distributed-systems
- Patrick Hunt, Mahadev Konar, Flavio P. Junqueira, and Benjamin Reed, "ZooKeeper: Wait-free Coordination for Internet-scale Systems", USENIX ATC 2010. https://www.usenix.org/conference/usenix-atc-10/zookeeper-wait-free-coordination-internet-scale-systems

A lease alone loses a pause. The holder is stopped past the deadline, a second worker claims, and the first holder writes anyway. Checking the clock immediately before the write does not close that gap, because the pause can sit between the check and the write. The store that accepts the write has to remember a strictly increasing token from the grant and reject a lower one.

`fence` increases only in the claim `UPDATE` that also sets `owner` and `lease_until`. Renewal, release, the intent insert, the error write, and completion all require `id`, `owner`, `fence`, and `lease_until > datetime('now')`. A token from the worker clock is not a fence. A random lock id is not a fence. `tests/test_fence.py` claims, writes the CSV, steps the clock past the deadline, lets a second owner reclaim, and then completes with the old fence. The update changes zero rows. The stored fence is the second owner's. The completion event for the old fence is absent. The same test rejects a stale renewal and a stale applied mark. Across the eight-process run, stale completions accepted are 0: each of the 100 jobs has one claim event, one completion event, and those two fences match.

Chubby's sequencer is the same check: an integer from acquisition that the resource rejects when it has gone backward. ZooKeeper's zxid or znode version is the precedent for taking the token from the grant order. This lab does not run a Chubby cell or a ZooKeeper ensemble. The per-job fence is the local form of that integer.

ZooKeeper's other point used here is that a slow client must not stall unrelated work. After the claim transaction commits, a worker blocked inside the CSV write does not hold the SQLite write lock. `tests/test_concurrency.py` claims job 2 while job 1 is still inside `perform`. Claim of job B waits only on its own row, not on job A's effect.

## Heartbeat, local jeopardy, and a zero-timeout release

Sources:

- Burrows, Chubby, same paper as above.
- Amazon SQS visibility timeout, same page as above.
- Kleppmann, same essay as above.

The lease is treated as a session. `renew` sets `lease_until` from `datetime('now')` plus the term under the live owner and fence. That is the local form of `ChangeMessageVisibility`. When the update changes zero rows, the worker sets a process-local jeopardy flag and does not start another effect and does not complete. Jeopardy is not a status column. `tests/test_fence.py` stops the renewal, advances the clock, and records zero effect calls. A second test sets jeopardy during the first effect failure and asserts the retry loop does not call `perform` again.

An expired holder cannot refresh itself. The renewal predicate requires the deadline to still be ahead of the database clock, which is Gray's rule that a conflicting grant waits out the current holder, applied per job.

Early release sets `lease_until` to `datetime('now')` and `status` back to `queued` under the same predicate. The attempt counted at claim time stays counted. `tests/test_lease.py` shows the next owner claims immediately inside a 30-second term. That matches SQS `VisibilityTimeout` 0. The worker starts a real heartbeat thread when `heartbeat_seconds` is greater than zero. `tests/test_heartbeat.py` runs that thread on the wall clock: a 3.5-second effect under a 3-second term completes with renewals and is rejected without them. The other tests call `renew` directly so they do not sleep. Heartbeats are per claim, so an effect that gave up one lease does not leave the next job unrenewed (`tests/test_recovery.py`).

## One-row claim, committed before the CSV

Sources:

- PostgreSQL Global Development Group, "PostgreSQL 17 SELECT", the locking clause. https://www.postgresql.org/docs/17/sql-select.html
- SQLite WAL, same page as above.
- Hunt et al., ZooKeeper, same paper as above.

The claim shape is one due row: `queued`, or `leased` with `lease_until` already past the database clock, `run_at` due, `attempts < max_attempts`, `ORDER BY run_at, id LIMIT 1`. The same statement sets `owner`, `fence = fence + 1`, `lease_until`, `attempts`, and `status = 'leased'`, and `RETURNING` gives the new fence. PostgreSQL's `FOR UPDATE SKIP LOCKED` is the documented queue form of that read: it skips rows locked by other consumers and deliberately returns an inconsistent view. SQLite WAL has one writer and does not have that clause. `BEGIN IMMEDIATE` is the mutual exclusion around the one-row update. The transaction commits before `perform`. Tests assert the global outcome (one live owner, one applied intent, one CSV) rather than one snapshot of the queue.

The eight-process test runs 100 jobs. Each ends `succeeded` with fence 1, one applied intent, and one CSV whose first data column is that job's idempotency key.

## Intent row for an effect that can outlive the transaction

Source: Pat Helland, "Life beyond Distributed Transactions: an Apostate's Opinion", CIDR 2007. https://www.cidrdb.org/cidr2007/papers/cidr07p15.pdf

The job row, the effect intent, and the job event are one SQLite entity. The CSV file is outside that entity. Delivery is at-least-once. The effect's idempotency key is the primary key of `effect_intents`. A second insert of that key is the success of the earlier attempt, not a new effect. The normal completion marks the intent `applied` and the job `succeeded` in one transaction, so a crash there rolls both back. A test fault commits the applied mark (still under the live owner and fence) and then raises before the job update, which is the split the recovery path has to finish without calling the effect again.

`tests/test_recovery.py` covers three cuts:

- after the intent insert and before the CSV: resume writes one file and completes under the same fence
- after the CSV and before the applied mark: resume sees the same bytes and does not create a second file
- after a committed applied mark and before job completion: resume completes and the effect call count stays 1

A schedule occurrence uses the key `sched:{schedule_id}:{slot_start}`. Submitting that slot again, including after the first row has succeeded, does not insert a second job (`tests/test_submit.py`).

A peer that ignores the key can still double-apply. The lab's `CsvEffect` does not: exclusive create, and an existing file with the same bytes is a replay. An existing file with different bytes is a conflict and is not retried.

## Idempotency-Key fingerprint on submit

Source: Jayadeba Jena and Sanjay Dalal, "The Idempotency-Key HTTP Header Field", draft-ietf-httpapi-idempotency-key-header-07, 15 October 2025. https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/

This is an expired Internet-Draft (it expired 18 April 2026), not an RFC. The CLI help says so. The fingerprint is `sha256-canonical-json-v1`: SHA-256 of the UTF-8 canonical JSON object with sorted keys and compact separators. The draft allows a checksum of the body; this is that choice.

- A missing key is rejected with a 400-class error and exit code 2.
- The same key and the same fingerprint, while the job is `queued` or `leased`, returns a 409-class conflict naming the original id. Attempts stay put and nothing is inserted. Exit code 4.
- The same key and a different fingerprint is a 422-class reject. Exit code 2. No second row.
- The same key and fingerprint after `succeeded` or `dead` returns the original id and the stored result or stored error.

Key order in the JSON object does not change the fingerprint. The retention window defaults to 7 days. `purge` deletes terminal rows only after that window, measured on the database clock. A purged key can be submitted again only because the record is gone. The test uses a 10-second window and a clock step of 11 seconds.

## Attempt budget and dead letter

Source: Amazon SQS visibility timeout, same page as above, including the dead-letter practice on that page.

`attempts` increments in the claim. An expiry before success is a failed attempt. When `attempts >= max_attempts` and the deadline has passed, `sweep_dead` sets `dead`, keeps `last_error`, and appends an event. Dead rows are not claimable. A successful completion is not swept, even when `max_attempts` is 1. A renewal does not increment attempts and does not dead-letter the row. Effect retries have their own cap (`effect_retry_cap`). Exhausting it records `last_error`, stops renewing that lease, and leaves it to expire into the claim budget. A row released back to `queued` on its last attempt is dead-lettered by the same sweep. A dead job's leftover `pending` intent does not keep a draining worker alive. `tests/test_deadletter.py` and `tests/test_recovery.py` cover those boundaries.

## Full jitter for empty polls, busy retries, and effect retries

Source: Marc Brooker, "Exponential Backoff And Jitter", AWS Architecture Blog, 2015. https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/

The helper is `sleep = random(0, min(cap, base * 2^attempt))`, drawn as a half-open uniform interval so the value can be zero and does not exceed the cap. Empty claims, `SQLITE_BUSY`, and effect retries call it. The sampled delay, the attempt, and the fence are on the log line. The initial grant adds `0..lease_jitter_seconds` whole seconds so a batch does not share one expiry tick (`tests/test_lease.py`).

`simulate_cohort` is a single-writer model in this repository, not Brooker's network simulator and not a measured SQLite timing. For seed `20261005`, 20 clients, 20 jobs, base 1, cap 32, and service 0.25, full jitter records 19 failed claims and synchronized backoff records 57. The synchronized drain time is 5.0 time units. The jitter drain time is shorter. Brooker's "more than half the calls" figure, at 100 clients with a 10 ms mean delay, stays his result. A May 2023 note on that page says many AWS SDKs now apply jitter in standard or adaptive retry mode. This process does not call those SDKs.

## Per-job event order and a stable owner id

Sources:

- Hunt et al., ZooKeeper, same paper as above.
- Burrows, Chubby, same paper as above.

Each worker picks one owner id for the process and writes it on the job at claim time. `job_events` rows are appended in the same transaction as claim, release, completion, and dead-letter. The primary key is `(job_id, seq)`. A reader that orders by `seq` sees the claim before the completion that names the same fence. That is the per-client FIFO observation, scoped to one job. It is not a cluster-wide zxid. Renewals are log lines, not event rows, so the table stays an audit of ownership changes. Every worker log line carries `job_id`, `owner`, `fence`, and `seq`.

## What this reading does not include

WAL does not run across hosts. Fencing the SQLite row does not fence a remote writer that ignores the idempotency key. The Idempotency-Key document is expired work in progress. Brooker's call-count reduction is from his simulator. Gray's paper budgets failure delay and clock error for file-cache leases; cache invalidation is not implemented. SQS can deliver a duplicate even inside the visibility window, and a holder that acts after expiry without the fence check can overlap a new owner here too. `datetime('now')` has one-second resolution, so grant jitter is a whole number of seconds. Quarantine fires when the leaseguard epoch is ahead of the recovered database, or when the guard file is corrupt. A lost commit that never reached the guard file is not distinguishable from a commit that did not happen.
