# Reliable Automation Job Runner Lab

A compact, fully offline Python automation runner. A declarative catalog describes a DAG of internal-ops jobs. The runner schedules a windowed pipeline, acquires per-job leases, executes handlers with retry/backoff, records structured logs, writes idempotent results, and checkpoints after each success. A simulated crash is recovered by replaying ledger hits and unfinished DAG nodes. Dry-run stages mutations in an overlay so later jobs in the same run can see them without committing durable state.

No network calls are made. Inbox records, catalogs, and fault scripts are synthetic.

## Problem

Internal automation fails in the gaps between "the cron fired and the script printed OK":

- jobs have dependencies, and a failed ingest must skip transform/export instead of writing a partial report
- overlapping hourly runs need a lease so two processes do not mutate the same inbox
- a crash after the handler committed but before the checkpoint saved must resume without duplicating work
- retries are required for timeouts and transient I/O, and must not retry validation errors
- poison rows belong in a dead-letter bucket; they must not fail a whole window
- operators need a dry-run that still walks the DAG and computes the same counts
- reruns in the same schedule window must replay stored results when the input fingerprint matches

This project is a standard-library-only sketch of that loop. It is a teaching/portfolio sample, not a production scheduler.

## Architecture

```
Catalog (JSON DAG)
  -> schema validation, unknown handlers, cycle detection
  -> window alignment (now_ms // window_ms), optional per-job interval and cron minute/hour gates
  -> JobRunner topo order
       for each job:
         skip if a dependency failed/was skipped, or the interval/cron gate is not due
         acquire lease (same run_id may re-enter; another holder is skipped)
         checkpoint current_job, then idempotency key (window | input_hash | run) + fingerprint
         replay on a successful ledger hit; conflict when the fingerprint differs
         handler with retry/backoff (timeout / transient_io retry; validation does not)
         atomic checkpoint of completed/failed/skipped job ids
         release lease
  -> crash injection (optional) at post_handler | post_checkpoint | post_lease_release
  -> resume from checkpoint + durable ledger + workspace
```

Package layout:

- `src/automation_job_lab/schema.py` — catalog/job/record contracts; rejects extra fields, unknown buckets, misaligned intervals, and non-finite JSON
- `src/automation_job_lab/schedule.py` — window alignment, per-job interval gate, and a UTC minute/hour cron subset
- `src/automation_job_lab/runner.py` — DAG orchestrator, crash injection, dry-run
- `src/automation_job_lab/handlers.py` — ingest, transform, export, notify, cleanup, heartbeat
- `src/automation_job_lab/retry.py` — retry classification and seeded exponential backoff
- `src/automation_job_lab/ledger.py` — idempotent execution records
- `src/automation_job_lab/checkpoint.py` — atomic JSON pipeline checkpoints
- `src/automation_job_lab/lease.py` — single-host leases with expiry
- `src/automation_job_lab/store.py` — workspace buckets with a dry-run overlay
- `src/automation_job_lab/telemetry.py` — manual clock, recording sleeper, structured JSON logger (timestamped events, optional JSON Lines stream)
- `examples/` — frozen catalog, inbox (including poison rows), and a one-shot fault script

Default topo order: `heartbeat-log`, `ingest-inbox`, `transform-records`, `export-report`, `cleanup-inbox`, `notify-ops`. Heartbeat is independent of the ingest chain so a leased or failed ingest still leaves an hourly ping.

## Run

From the repository root (Python 3.10+, standard library only, no network access needed):

```text
python -m unittest discover -s projects/automation-job-runner-lab/tests -v
```

Offline demo against the synthetic inbox:

```text
python projects/automation-job-runner-lab/run_lab.py
python projects/automation-job-runner-lab/run_lab.py --dry-run
python projects/automation-job-runner-lab/run_lab.py --state-dir <state-dir> --crash-after-jobs 2
python projects/automation-job-runner-lab/run_lab.py --state-dir <state-dir>
python projects/automation-job-runner-lab/run_lab.py --until-windows 2 --faults <empty-faults.json>
python projects/automation-job-runner-lab/run_lab.py --now-ms 1767226380000
python projects/automation-job-runner-lab/run_lab.py --log-jsonl 2> <log-file>
```

`<state-dir>` is any writable directory outside the repository (for example a temp folder). Without `--state-dir` the CLI creates a fresh temp directory and prints its path. `--dry-run` keeps all state in memory, creates no directory, and reports `"state_dir": null`.

Logging: every event is a flat JSON object with `event`, `ts_ms` (from the injectable clock), and event fields such as `job_id`, `attempt`, `code`, and `key`. `--log-jsonl` streams events to stderr as JSON Lines while the run happens; `--print-events` embeds them in the final stdout report.

The default clock is `2026-01-01T00:00:00Z` (`1767225600000` ms). The default demo loads `examples/fault_script.json`: a timeout on ingest attempt 1 and transient I/O on transform attempt 1. The report shows `retries: 2`, `workspace.clean: 10`, `dead_letter: 2`, `archive: 10`, `inbox: 2`. The two poison rows (`REC-1098` extra field, `REC-1099` amount mismatch) stay in the inbox and the dead-letter bucket; only validated records are archived.

`--until-windows 2` advances one hour after the first complete window and inserts three more synthetic inbox rows (`REC-2001`..`REC-2003`). `--now-ms` 13 minutes after the epoch skips `heartbeat-log` (`cron_minute: 0`) while the rest of the DAG still runs.

Exit codes: `0` every window completed, `4` the run finished but at least one window has a failed job (the JSON report is still printed and `failed_windows` counts them, so a cron wrapper can alert), `3` simulated crash (durable workspace, ledger, lease, and checkpoint are left behind; rerun with the same `--state-dir` and no crash flag to resume), `2` missing/malformed `--catalog` / `--inbox` / `--faults` files or out-of-range arguments, `1` a runtime lab error such as a corrupt checkpoint or ledger. Each prints a one-line message rather than a traceback.

## Design decisions

- **Declarative catalog.** Jobs name a handler, DAG edges, timeout, retry policy, schedule, and idempotency mode. Unknown handlers, extra fields, unknown workspace buckets, self-edges, missing dependencies, and cycles fail at load time.
- **Window-aligned runs.** `run_id` is `{pipeline}:w{window_start_ms}`. A complete checkpoint for that id is a no-op on resume. `--force` re-walks the DAG; the ledger still replays matching fingerprints.
- **Schedules are gates, not a calendar server.** A job with no schedule runs every pipeline window. `every_ms` / `offset_ms` (multiples of `window_ms`, checked at load time) make a job run only in the windows that open its own interval, e.g. `every_ms: 7200000` runs every other hourly window. `cron_minute` / `cron_hour` are UTC fields on the injectable clock. A job that is not due is skipped with `not_due`; its dependents skip with `dependency_skipped` and the window still completes.
- **Three idempotency modes.** `window` keys a job to the schedule window, `input_hash` keys transform to the current source id/version pairs, `run` keys notify to the run id. A successful ledger row with a different fingerprint is `idempotency_conflict` and is not retried. Window/run fingerprints are handler plus params so a later inbox mutation in the same window still replays. Because `input_hash` keys include versions, an updated record in a later window gets a fresh key and is reprocessed rather than wedging the pipeline on a conflict.
- **Failed executions are not success.** A failed ledger row is overwritten by a later attempt with the same key.
- **Retry only what is safe.** `retry.is_retryable` is the single classifier: `timeout` and `transient_io` retry with exponential backoff and seeded jitter; `validation_error`, schema failures, and idempotency conflicts fail the job immediately. State/checkpoint write errors are not job outcomes; they abort the run (exit `1`). Jitter is seeded from `{seed}:{job_id}:{run_id}` so tests are deterministic.
- **Poison rows vs handler failure.** Transform dead-letters invalid records and still succeeds. An injected `validation` fault fails the job; dependents skip with `dependency_failed`.
- **Checkpoint after success.** The checkpoint records `current_job` before a handler starts, so a crash leaves the interrupted job on disk and the resumed `pipeline_start` event reports it as `interrupted_job`. Crash `post_handler` leaves the ledger written and the lease held; resume replays the job and then checkpoints. Crash `post_checkpoint` resumes at the next DAG node. Crash `post_lease_release` is a clean cut between jobs.
- **Leases.** Another holder with an unexpired lease causes a `leased` skip (dependents skip with `dependency_skipped`). The same `run_id` may reacquire after a crash. Expired leases can be taken by a later run. The TTL covers every attempt's timeout plus the maximum backoff and jitter between attempts.
- **Dry-run stages, never commits.** Handler writes go to a discardable overlay so later jobs in the same dry run see earlier ones. Checkpoints, ledgers, leases, and workspace files are not written.

## Limitations

- There is no distributed scheduler, worker pool, or subprocess isolation in this repository.
- Backoff sleep is injectable. The default `WallClock` really sleeps; the CLI and tests use a recording sleeper that advances a `ManualClock`.
- Durable state is JSON files (or in-memory). They are not a database and they are not multi-process-safe beyond `os.replace` on a single host.
- Timeouts are cooperative: handlers check `deadline_ms` between rows and after injected delays. A handler stuck inside a single blocking call would not be interrupted, because there is no thread or subprocess isolation.
- The cron subset is UTC `minute` and optional `hour` only. It does not parse crontab strings or time zones. Interval and cron gates are evaluated only when the runner is invoked; nothing here is a long-lived daemon.
- Dry-run always starts from the provided inbox in memory; it does not read an existing `--state-dir` workspace.
- This is a teaching/portfolio sample, not a production job system.

## What it demonstrates

- Declarative DAG jobs with schema validation and cycle detection
- Window-aligned scheduling with per-job interval and cron minute gates
- Exponential backoff, seeded jitter, and non-retryable validation errors
- Idempotent execution keys, fingerprint conflicts, and replay after a lost checkpoint
- Atomic checkpoints and crash recovery that does not duplicate archived records
- Single-host leases against overlapping runs
- Timestamped structured JSON logs (in memory or streamed as JSON Lines), dry-run overlays, poison-row dead letters, and offline unittest coverage
- CLI exit codes a scheduler wrapper can act on (`0` ok, `4` failed jobs, `3` simulated crash, `2` bad input, `1` state error)
