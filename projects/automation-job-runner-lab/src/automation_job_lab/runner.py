"""DAG runner with leases, retries, checkpoints, and dry-run."""

from __future__ import annotations

from typing import Any, Callable, Optional
import hashlib
import json

from .checkpoint import MemoryCheckpointStore
from .errors import LabError, PermanentError, SchemaError, SimulatedCrash, ValidationError
from .handlers import FaultInjector, HandlerContext, dispatch
from .ledger import ExecutionLedger
from .lease import LeaseStore
from .models import (
    CRASH_AT,
    Catalog,
    Checkpoint,
    ExecutionRecord,
    JobResult,
    JobSpec,
    RunReport,
)
from .retry import RetryPolicy, backoff_ms, is_retryable, rng_for
from .schedule import cron_due, interval_due, window_start
from .schema import topo_sort
from .store import Workspace
from .telemetry import JsonLogger, WallClock


class JobRunner:
    def __init__(
        self,
        catalog: Catalog,
        *,
        workspace: Optional[Workspace] = None,
        ledger: Optional[ExecutionLedger] = None,
        store: Any = None,
        leases: Optional[LeaseStore] = None,
        logger: Optional[JsonLogger] = None,
        clock: Any = None,
        sleeper: Optional[Callable[[int], None]] = None,
        faults: Optional[FaultInjector] = None,
        seed: int = 7,
        crash_after_jobs: Optional[int] = None,
        crash_at: str = "post_handler",
        fail_fast: bool = False,
    ) -> None:
        if crash_at not in CRASH_AT:
            raise ValueError(f"unknown crash_at {crash_at!r}")
        self.catalog = catalog
        self.workspace = workspace if workspace is not None else Workspace()
        self.ledger = ledger if ledger is not None else ExecutionLedger()
        self.store = store if store is not None else MemoryCheckpointStore()
        self.leases = leases if leases is not None else LeaseStore()
        self.clock = clock if clock is not None else WallClock()
        self.logger = logger if logger is not None else JsonLogger(clock=self.clock)
        self.sleeper = sleeper if sleeper is not None else self.clock.sleep
        self.faults = faults if faults is not None else FaultInjector()
        self.seed = int(seed)
        self.crash_after_jobs = crash_after_jobs
        self.crash_at = crash_at
        self.fail_fast = fail_fast
        self._successes_this_call = 0
        self._retry_count = 0
        self.order = topo_sort(catalog.jobs)

    def run(self, *, dry_run: bool = False, resume: bool = True, force: bool = False) -> RunReport:
        self.workspace.discard_staged()
        self.ledger.discard_staged()
        self._successes_this_call = 0
        self._retry_count = 0
        now = self.clock.now_ms()
        window = window_start(now, self.catalog.window_ms)
        run_id = f"{self.catalog.pipeline}:w{window}"

        existing = self.store.load(run_id) if resume else None
        if existing is not None and existing.status == "complete" and not force:
            self.logger.log("pipeline_already_complete", run_id=run_id, window_start_ms=window)
            return RunReport(
                pipeline=self.catalog.pipeline,
                run_id=run_id,
                dry_run=dry_run,
                resumed=True,
                status="complete",
                window_start_ms=window,
                jobs=[
                    JobResult(job_id=job.id, status="already_complete", replayed=True)
                    for job in self.order
                ],
                checkpoint=existing.to_dict(),
            )

        resumed = existing is not None
        if force and existing is not None and existing.status == "complete":
            checkpoint = Checkpoint(
                pipeline=self.catalog.pipeline,
                run_id=run_id,
                window_start_ms=window,
                status="in_progress",
            )
            resumed = False
        else:
            checkpoint = (
                existing
                if existing is not None
                else Checkpoint(
                    pipeline=self.catalog.pipeline,
                    run_id=run_id,
                    window_start_ms=window,
                    status="in_progress",
                )
            )
        interrupted_job = checkpoint.current_job
        checkpoint.status = "in_progress"
        report = RunReport(
            pipeline=self.catalog.pipeline,
            run_id=run_id,
            dry_run=dry_run,
            resumed=resumed,
            status="in_progress",
            window_start_ms=window,
            force=force,
        )
        self._persist(checkpoint, dry_run=dry_run)
        self.logger.log(
            "pipeline_start",
            run_id=run_id,
            dry_run=dry_run,
            resumed=resumed,
            force=force,
            window_start_ms=window,
            interrupted_job=interrupted_job,
        )

        try:
            for job in self.order:
                result = self._run_one(
                    job,
                    checkpoint,
                    dry_run=dry_run,
                    force=force,
                    run_id=run_id,
                    window=window,
                )
                report.jobs.append(result)
                if self.fail_fast and result.status == "failed":
                    break
            if any(item.status == "failed" for item in report.jobs):
                checkpoint.status = "failed"
                report.status = "failed"
            else:
                checkpoint.status = "complete"
                report.status = "complete"
            checkpoint.current_job = None
            self._persist(checkpoint, dry_run=dry_run)
            self.logger.log("pipeline_complete", run_id=run_id, status=report.status)
        except SimulatedCrash:
            checkpoint.status = "in_progress"
            self.logger.log(
                "pipeline_crash",
                run_id=run_id,
                completed_jobs=list(checkpoint.completed_jobs),
                current_job=checkpoint.current_job,
                crash_at=self.crash_at,
            )
            raise

        report.checkpoint = checkpoint.to_dict()
        report.retries = self._retry_count
        return report

    def _run_one(
        self,
        job: JobSpec,
        checkpoint: Checkpoint,
        *,
        dry_run: bool,
        force: bool,
        run_id: str,
        window: int,
    ) -> JobResult:
        if job.id in checkpoint.completed_jobs:
            return JobResult(job_id=job.id, status="already_complete", replayed=True)
        if job.id in checkpoint.failed_jobs:
            checkpoint.failed_jobs = [item for item in checkpoint.failed_jobs if item != job.id]
        if job.id in checkpoint.skipped_jobs:
            checkpoint.skipped_jobs = [item for item in checkpoint.skipped_jobs if item != job.id]

        unfinished = [dep for dep in job.depends_on if dep not in checkpoint.completed_jobs]
        if unfinished:
            failed = any(dep in checkpoint.failed_jobs for dep in unfinished)
            reason = "dependency_failed" if failed else "dependency_skipped"
            checkpoint.skipped_jobs.append(job.id)
            self._persist(checkpoint, dry_run=dry_run)
            self.logger.log("job_skipped", job_id=job.id, reason=reason, blocked=unfinished)
            return JobResult(job_id=job.id, status="skipped", reason=reason)

        if not force and not self._is_due(job, window):
            checkpoint.skipped_jobs.append(job.id)
            self._persist(checkpoint, dry_run=dry_run)
            self.logger.log("job_skipped", job_id=job.id, reason="not_due")
            return JobResult(job_id=job.id, status="skipped", reason="not_due")

        lease_acquired = False
        ttl = lease_ttl_ms(job)
        if not dry_run:
            if not self.leases.acquire(job.id, run_id, self.clock.now_ms(), ttl):
                checkpoint.skipped_jobs.append(job.id)
                self._persist(checkpoint, dry_run=dry_run)
                self.logger.log("lease_held", job_id=job.id, run_id=run_id)
                return JobResult(job_id=job.id, status="skipped", reason="leased")
            lease_acquired = True
            self.logger.log("lease_acquired", job_id=job.id, holder=run_id, ttl_ms=ttl)

        # Persisted so a crash before the next checkpoint names the interrupted job.
        checkpoint.current_job = job.id
        self._persist(checkpoint, dry_run=dry_run)
        crashed = False
        try:
            result = self._execute_job(job, run_id=run_id, window=window, dry_run=dry_run)
            if result.status in {"succeeded", "replayed"}:
                if result.status == "succeeded":
                    self._bump_and_maybe_crash("post_handler")
                checkpoint.current_job = None
                checkpoint.completed_jobs.append(job.id)
                self._persist(checkpoint, dry_run=dry_run)
                if result.status == "succeeded":
                    self._bump_and_maybe_crash("post_checkpoint")
            elif result.status == "failed":
                checkpoint.current_job = None
                checkpoint.failed_jobs.append(job.id)
                self._persist(checkpoint, dry_run=dry_run)
            if lease_acquired:
                self.leases.release(job.id, run_id)
                lease_acquired = False
                self.logger.log("lease_released", job_id=job.id, holder=run_id)
            if result.status == "succeeded":
                self._bump_and_maybe_crash("post_lease_release")
            return result
        except SimulatedCrash:
            crashed = True
            raise
        finally:
            if not crashed:
                if lease_acquired:
                    self.leases.release(job.id, run_id)
                    self.logger.log("lease_released", job_id=job.id, holder=run_id)
                checkpoint.current_job = None

    def _execute_job(
        self,
        job: JobSpec,
        *,
        run_id: str,
        window: int,
        dry_run: bool,
    ) -> JobResult:
        key = make_key(self.catalog.pipeline, job, run_id, window, self.workspace)
        fingerprint = make_fingerprint(job, self.workspace)
        existing = self.ledger.get(key, dry_run=dry_run)
        if existing is not None and existing.status == "succeeded":
            if existing.fingerprint != fingerprint:
                self.logger.log(
                    "job_failed",
                    job_id=job.id,
                    reason="idempotency_conflict",
                    key=key,
                )
                return JobResult(
                    job_id=job.id,
                    status="failed",
                    reason="idempotency_conflict",
                )
            self.logger.log("job_replayed", job_id=job.id, key=key)
            return JobResult(
                job_id=job.id,
                status="replayed",
                attempts=existing.attempt,
                replayed=True,
                output=dict(existing.output),
            )

        policy = RetryPolicy.from_spec(job.retry)
        rng = rng_for(self.seed, job.id, run_id)
        started = self.clock.now_ms()
        last_error: Optional[LabError] = None
        attempts = policy.max_attempts
        for attempt in range(1, policy.max_attempts + 1):
            self.logger.log("job_start", job_id=job.id, attempt=attempt, key=key, dry_run=dry_run)
            try:
                ctx = HandlerContext(
                    job=job,
                    workspace=self.workspace,
                    clock=self.clock,
                    logger=self.logger,
                    sleeper=self.sleeper,
                    faults=self.faults,
                    dry_run=dry_run,
                    attempt=attempt,
                    run_id=run_id,
                    window_start_ms=window,
                    deadline_ms=self.clock.now_ms() + job.timeout_ms,
                    pipeline=self.catalog.pipeline,
                )
                output = dispatch(job.handler, ctx)
                finished = self.clock.now_ms()
                record = ExecutionRecord(
                    key=key,
                    job_id=job.id,
                    status="succeeded",
                    attempt=attempt,
                    fingerprint=fingerprint,
                    output=dict(output),
                    started_ms=started,
                    finished_ms=finished,
                )
                self.ledger.put(record, dry_run=dry_run)
                self.logger.log("job_succeeded", job_id=job.id, attempt=attempt, key=key)
                return JobResult(
                    job_id=job.id,
                    status="succeeded",
                    attempts=attempt,
                    output=dict(output),
                    duration_ms=finished - started,
                )
            except SimulatedCrash:
                raise
            except LabError as exc:
                if is_retryable(exc):
                    last_error = exc
                    self.logger.log(
                        "job_retry",
                        job_id=job.id,
                        attempt=attempt,
                        code=exc.code,
                        message=exc.message,
                    )
                    if attempt >= policy.max_attempts:
                        break
                    delay = backoff_ms(policy, attempt, rng)
                    self.sleeper(delay)
                    self._retry_count += 1
                    continue
                if not isinstance(exc, (PermanentError, SchemaError)):
                    # State/checkpoint write failures are infrastructure errors, not job outcomes.
                    raise
                code = exc.code
                finished = self.clock.now_ms()
                self.logger.log(
                    "job_failed",
                    job_id=job.id,
                    attempt=attempt,
                    code=code,
                    retryable=False,
                )
                self.ledger.put(
                    ExecutionRecord(
                        key=key,
                        job_id=job.id,
                        status="failed",
                        attempt=attempt,
                        fingerprint=fingerprint,
                        output={"error": exc.message},
                        started_ms=started,
                        finished_ms=finished,
                    ),
                    dry_run=dry_run,
                )
                return JobResult(
                    job_id=job.id,
                    status="failed",
                    attempts=attempt,
                    reason=code,
                    duration_ms=finished - started,
                )
        finished = self.clock.now_ms()
        reason = last_error.code if last_error is not None else "exhausted"
        message = last_error.message if last_error is not None else "retries exhausted"
        self.logger.log(
            "job_failed",
            job_id=job.id,
            attempt=attempts,
            code=reason,
            retryable=True,
            exhausted=True,
        )
        self.ledger.put(
            ExecutionRecord(
                key=key,
                job_id=job.id,
                status="failed",
                attempt=attempts,
                fingerprint=fingerprint,
                output={"error": message},
                started_ms=started,
                finished_ms=finished,
            ),
            dry_run=dry_run,
        )
        return JobResult(
            job_id=job.id,
            status="failed",
            attempts=attempts,
            reason=reason,
            duration_ms=finished - started,
        )

    def _is_due(self, job: JobSpec, window: int) -> bool:
        spec = job.schedule
        if spec.every_ms is not None and not interval_due(spec, window):
            return False
        if spec.cron_minute is not None and not cron_due(spec, last_success_ms=None, now_ms=self.clock.now_ms()):
            return False
        return True

    def _bump_and_maybe_crash(self, at: str) -> None:
        if at != self.crash_at:
            return
        self._successes_this_call += 1
        if self.crash_after_jobs is not None and self._successes_this_call >= self.crash_after_jobs:
            raise SimulatedCrash(
                f"crash after {self._successes_this_call} jobs at {self.crash_at}"
            )

    def _persist(self, checkpoint: Checkpoint, *, dry_run: bool) -> None:
        checkpoint.updated_ms = self.clock.now_ms()
        if dry_run:
            return
        self.store.save(checkpoint)
        self.logger.log(
            "checkpoint_saved",
            run_id=checkpoint.run_id,
            status=checkpoint.status,
            completed_jobs=list(checkpoint.completed_jobs),
        )


def lease_ttl_ms(job: JobSpec) -> int:
    retries = max(0, job.retry.max_attempts - 1)
    delay_budget = (job.retry.max_delay_ms + job.retry.jitter_ms) * retries
    return job.timeout_ms * job.retry.max_attempts + delay_budget + 100


def make_key(
    pipeline: str,
    job: JobSpec,
    run_id: str,
    window: int,
    workspace: Workspace,
) -> str:
    mode = job.idempotency.mode
    if mode == "window":
        return f"{pipeline}:{job.id}:w{window}"
    if mode == "run":
        return f"{pipeline}:{job.id}:{run_id}"
    if mode == "input_hash":
        # id:version pairs, so an updated record gets a new key instead of a conflict.
        pairs = _source_versions(job, workspace)
        digest = hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()[:16]
        return f"{pipeline}:{job.id}:h{digest}"
    raise PermanentError("bad_request", f"unknown idempotency mode {mode}")


def make_fingerprint(job: JobSpec, workspace: Workspace) -> str:
    payload: dict[str, object] = {"handler": job.handler, "params": job.params}
    if job.idempotency.mode == "input_hash":
        payload["source"] = _source_versions(job, workspace)
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _source_versions(job: JobSpec, workspace: Workspace) -> list[str]:
    source = job.params.get("source")
    if not isinstance(source, str) or not source:
        return []
    try:
        rows = workspace.list(source)
    except ValidationError:
        return []
    return sorted(f"{row.get('id')}:{row.get('version')}" for row in rows)
