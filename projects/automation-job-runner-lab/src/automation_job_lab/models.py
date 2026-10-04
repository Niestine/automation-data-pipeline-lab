"""Domain objects for the automation job runner lab."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# 2026-01-01T00:00:00Z. Tests and the default CLI clock start here so cron
# minute 0 matches the first pipeline window.
LAB_EPOCH_MS = 1_767_225_600_000

HANDLERS = ("ingest", "transform", "export", "notify", "cleanup", "heartbeat")
RECORD_KINDS = ("invoice", "ticket", "file_index", "report")
RECORD_STATUSES = ("pending", "ready", "processed", "failed")
JOB_RESULT_STATUSES = (
    "succeeded",
    "replayed",
    "failed",
    "skipped",
    "already_complete",
)
PIPELINE_STATUSES = ("in_progress", "complete", "failed")
IDEMPOTENCY_MODES = ("window", "input_hash", "run")
CURRENCIES = ("USD", "EUR", "JPY")
TICKET_QUEUES = ("ops", "support", "billing")
RECORD_BUCKETS = ("inbox", "staging", "clean", "dead_letter", "archive")
BLOB_BUCKETS = ("exports", "notifications", "heartbeats")
CRASH_AT = ("post_handler", "post_checkpoint", "post_lease_release")
FAULT_ERRORS = ("timeout", "transient_io", "validation")

ISO_Z = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
ID_PATTERN = r"^[a-z][a-z0-9-]{1,62}$"
RECORD_ID_PATTERN = r"^REC-[0-9]{4,}$"


@dataclass(frozen=True)
class RetrySpec:
    max_attempts: int = 4
    base_delay_ms: int = 10
    max_delay_ms: int = 200
    multiplier: float = 2.0
    jitter_ms: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_validated(cls, data: dict[str, Any]) -> RetrySpec:
        return cls(
            max_attempts=int(data["max_attempts"]),
            base_delay_ms=int(data["base_delay_ms"]),
            max_delay_ms=int(data["max_delay_ms"]),
            multiplier=float(data["multiplier"]),
            jitter_ms=int(data["jitter_ms"]),
        )


@dataclass(frozen=True)
class ScheduleSpec:
    every_ms: Optional[int] = None
    offset_ms: int = 0
    cron_minute: Optional[int] = None
    cron_hour: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.every_ms is not None:
            payload["every_ms"] = self.every_ms
        if self.offset_ms:
            payload["offset_ms"] = self.offset_ms
        if self.cron_minute is not None:
            payload["cron_minute"] = self.cron_minute
        if self.cron_hour is not None:
            payload["cron_hour"] = self.cron_hour
        return payload

    @classmethod
    def from_validated(cls, data: dict[str, Any]) -> ScheduleSpec:
        return cls(
            every_ms=data.get("every_ms"),
            offset_ms=int(data["offset_ms"]) if data.get("offset_ms") is not None else 0,
            cron_minute=data.get("cron_minute"),
            cron_hour=data.get("cron_hour"),
        )


@dataclass(frozen=True)
class IdempotencySpec:
    mode: str = "window"

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}

    @classmethod
    def from_validated(cls, data: dict[str, Any]) -> IdempotencySpec:
        return cls(mode=str(data["mode"]))


@dataclass(frozen=True)
class JobSpec:
    id: str
    handler: str
    schedule: ScheduleSpec
    retry: RetrySpec
    timeout_ms: int
    depends_on: tuple[str, ...]
    idempotency: IdempotencySpec
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "handler": self.handler,
            "schedule": self.schedule.to_dict(),
            "retry": self.retry.to_dict(),
            "timeout_ms": self.timeout_ms,
            "depends_on": list(self.depends_on),
            "idempotency": self.idempotency.to_dict(),
            "params": dict(self.params),
        }

    @classmethod
    def from_validated(cls, data: dict[str, Any]) -> JobSpec:
        return cls(
            id=str(data["id"]),
            handler=str(data["handler"]),
            schedule=ScheduleSpec.from_validated(data.get("schedule") or {}),
            retry=RetrySpec.from_validated(data["retry"]),
            timeout_ms=int(data["timeout_ms"]),
            depends_on=tuple(str(item) for item in data.get("depends_on") or ()),
            idempotency=IdempotencySpec.from_validated(data.get("idempotency") or {"mode": "window"}),
            params=dict(data.get("params") or {}),
        )


@dataclass(frozen=True)
class Catalog:
    pipeline: str
    window_ms: int
    jobs: tuple[JobSpec, ...]

    def job(self, job_id: str) -> JobSpec:
        for item in self.jobs:
            if item.id == job_id:
                return item
        raise KeyError(job_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "window_ms": self.window_ms,
            "jobs": [job.to_dict() for job in self.jobs],
        }


@dataclass
class Checkpoint:
    pipeline: str
    run_id: str
    status: str = "in_progress"
    window_start_ms: int = 0
    completed_jobs: list[str] = field(default_factory=list)
    failed_jobs: list[str] = field(default_factory=list)
    skipped_jobs: list[str] = field(default_factory=list)
    current_job: Optional[str] = None
    updated_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "run_id": self.run_id,
            "status": self.status,
            "window_start_ms": self.window_start_ms,
            "completed_jobs": list(self.completed_jobs),
            "failed_jobs": list(self.failed_jobs),
            "skipped_jobs": list(self.skipped_jobs),
            "current_job": self.current_job,
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        if not isinstance(data, dict):
            raise ValueError("checkpoint must be an object")
        pipeline = str(data.get("pipeline") or "").strip()
        run_id = str(data.get("run_id") or "").strip()
        if not pipeline:
            raise ValueError("checkpoint.pipeline is required")
        if not run_id:
            raise ValueError("checkpoint.run_id is required")
        status = str(data.get("status") or "in_progress")
        if status not in PIPELINE_STATUSES:
            raise ValueError(f"checkpoint.status {status!r} is invalid")
        window_start_ms = data.get("window_start_ms", 0)
        if type(window_start_ms) is not int or window_start_ms < 0:
            raise ValueError("checkpoint.window_start_ms must be a non-negative integer")
        updated_ms = data.get("updated_ms", 0)
        if type(updated_ms) is not int or updated_ms < 0:
            raise ValueError("checkpoint.updated_ms must be a non-negative integer")
        current_job = data.get("current_job")
        if current_job is not None:
            current_job = str(current_job)
        return cls(
            pipeline=pipeline,
            run_id=run_id,
            status=status,
            window_start_ms=window_start_ms,
            completed_jobs=_string_list(data.get("completed_jobs"), "completed_jobs"),
            failed_jobs=_string_list(data.get("failed_jobs"), "failed_jobs"),
            skipped_jobs=_string_list(data.get("skipped_jobs"), "skipped_jobs"),
            current_job=current_job,
            updated_ms=updated_ms,
        )


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"checkpoint.{field_name} must be an array of strings")
    return list(value)


@dataclass
class ExecutionRecord:
    key: str
    job_id: str
    status: str
    attempt: int
    fingerprint: str
    output: dict[str, Any]
    started_ms: int
    finished_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "job_id": self.job_id,
            "status": self.status,
            "attempt": self.attempt,
            "fingerprint": self.fingerprint,
            "output": dict(self.output),
            "started_ms": self.started_ms,
            "finished_ms": self.finished_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionRecord:
        if not isinstance(data, dict):
            raise ValueError("execution record must be an object")
        key = str(data.get("key") or "").strip()
        job_id = str(data.get("job_id") or "").strip()
        status = str(data.get("status") or "").strip()
        fingerprint = str(data.get("fingerprint") or "")
        if not key or not job_id or status not in {"succeeded", "failed"}:
            raise ValueError("execution record is missing key, job_id, or a valid status")
        attempt = data.get("attempt", 0)
        started_ms = data.get("started_ms", 0)
        finished_ms = data.get("finished_ms", 0)
        if type(attempt) is not int or attempt < 0:
            raise ValueError("execution record.attempt must be a non-negative integer")
        if type(started_ms) is not int or type(finished_ms) is not int:
            raise ValueError("execution record timestamps must be integers")
        output = data.get("output") or {}
        if not isinstance(output, dict):
            raise ValueError("execution record.output must be an object")
        return cls(
            key=key,
            job_id=job_id,
            status=status,
            attempt=attempt,
            fingerprint=fingerprint,
            output=dict(output),
            started_ms=started_ms,
            finished_ms=finished_ms,
        )


@dataclass
class JobResult:
    job_id: str
    status: str
    attempts: int = 0
    reason: Optional[str] = None
    replayed: bool = False
    output: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "attempts": self.attempts,
            "reason": self.reason,
            "replayed": self.replayed,
            "output": dict(self.output),
            "duration_ms": self.duration_ms,
        }


@dataclass
class RunReport:
    pipeline: str
    run_id: str
    dry_run: bool
    resumed: bool
    status: str
    window_start_ms: int
    jobs: list[JobResult] = field(default_factory=list)
    retries: int = 0
    force: bool = False
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def job_map(self) -> dict[str, JobResult]:
        return {item.job_id: item for item in self.jobs}

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "resumed": self.resumed,
            "status": self.status,
            "window_start_ms": self.window_start_ms,
            "jobs": [item.to_dict() for item in self.jobs],
            "retries": self.retries,
            "force": self.force,
            "checkpoint": dict(self.checkpoint),
        }


@dataclass
class Lease:
    job_id: str
    holder: str
    expires_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "holder": self.holder, "expires_ms": self.expires_ms}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lease:
        if not isinstance(data, dict):
            raise ValueError("lease must be an object")
        job_id = str(data.get("job_id") or "").strip()
        holder = str(data.get("holder") or "").strip()
        expires_ms = data.get("expires_ms", 0)
        if not job_id or not holder:
            raise ValueError("lease.job_id and lease.holder are required")
        if type(expires_ms) is not int:
            raise ValueError("lease.expires_ms must be an integer")
        return cls(job_id=job_id, holder=holder, expires_ms=expires_ms)
