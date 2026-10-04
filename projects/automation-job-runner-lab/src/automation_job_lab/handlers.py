"""Synthetic job handlers and one-shot fault injection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import re

from .errors import JobTimeout, PermanentError, TransientError, ValidationError
from .models import FAULT_ERRORS, JobSpec
from .schema import validate_record
from .store import Workspace


NAME_TEMPLATE_OK = re.compile(r"^[A-Za-z0-9{}_.-]+$")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass
class Fault:
    job_id: str
    attempt: Optional[int] = None
    error: Optional[str] = None
    sleep_ms: int = 0
    consumed: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fault:
        if not isinstance(data, dict):
            raise ValueError("fault must be an object")
        extra = sorted(set(data) - {"job_id", "attempt", "error", "sleep_ms"})
        if extra:
            raise ValueError(f"unknown fault fields: {extra}")
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("fault.job_id is required")
        attempt = data.get("attempt")
        if attempt is not None and (type(attempt) is not int or attempt < 1 or attempt > 10):
            raise ValueError("fault.attempt must be an integer 1..10")
        error = data.get("error")
        if error is not None:
            error = str(error)
            if error not in FAULT_ERRORS:
                raise ValueError(f"fault.error {error!r} is invalid")
        sleep_ms = data.get("sleep_ms", 0)
        if type(sleep_ms) is not int or sleep_ms < 0 or sleep_ms > 60_000:
            raise ValueError("fault.sleep_ms must be an integer 0..60000")
        if error is None and sleep_ms == 0:
            raise ValueError("fault needs error or sleep_ms")
        return cls(job_id=job_id, attempt=attempt, error=error, sleep_ms=sleep_ms)


@dataclass
class FaultInjector:
    faults: list[Fault] = field(default_factory=list)

    def consume(self, job_id: str, attempt: int) -> Optional[Fault]:
        for fault in self.faults:
            if fault.consumed:
                continue
            if fault.job_id != job_id:
                continue
            if fault.attempt is not None and fault.attempt != attempt:
                continue
            fault.consumed = True
            return fault
        return None


@dataclass
class HandlerContext:
    job: JobSpec
    workspace: Workspace
    clock: Any
    logger: Any
    sleeper: Callable[[int], None]
    faults: FaultInjector
    dry_run: bool
    attempt: int
    run_id: str
    window_start_ms: int
    deadline_ms: int
    pipeline: str

    def apply_faults(self) -> None:
        fault = self.faults.consume(self.job.id, self.attempt)
        if fault is not None and fault.sleep_ms:
            self.sleeper(fault.sleep_ms)
        self.check_deadline()
        if fault is None or not fault.error:
            return
        if fault.error == "timeout":
            raise JobTimeout(f"injected timeout for {self.job.id}")
        if fault.error == "transient_io":
            raise TransientError(f"injected transient I/O for {self.job.id}")
        if fault.error == "validation":
            raise ValidationError(f"injected validation error for {self.job.id}")
        raise ValidationError(f"unknown injected error {fault.error}")

    def check_deadline(self) -> None:
        if self.clock.now_ms() >= self.deadline_ms:
            raise JobTimeout(f"handler {self.job.id} exceeded timeout_ms")


def render_name(template: str, **fields: Any) -> str:
    if not NAME_TEMPLATE_OK.fullmatch(template):
        raise PermanentError("bad_request", "export name must be a basename token")
    if ".." in template or "/" in template or "\\" in template:
        raise PermanentError("bad_request", "export name must be a basename")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in fields:
            raise PermanentError("bad_request", f"unknown name placeholder {{{key}}}")
        return str(fields[key])

    rendered = PLACEHOLDER.sub(replace, template)
    if "{" in rendered or "}" in rendered:
        raise PermanentError("bad_request", "unresolved name placeholder")
    return rendered


def ingest(ctx: HandlerContext) -> dict[str, Any]:
    ctx.apply_faults()
    source = str(ctx.job.params["source"])
    dest = str(ctx.job.params["dest"])
    inserted = updated = ignored = 0
    for row in ctx.workspace.list(source):
        ctx.check_deadline()
        if not isinstance(row, dict) or not str(row.get("id") or "").strip():
            raise ValidationError(f"ingest source {source} has a row without id")
        outcome = ctx.workspace.upsert(dest, row, dry_run=ctx.dry_run)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            ignored += 1
    ctx.logger.log(
        "ingest_done",
        job_id=ctx.job.id,
        inserted=inserted,
        updated=updated,
        ignored=ignored,
    )
    return {"inserted": inserted, "updated": updated, "ignored": ignored, "count": inserted + updated + ignored}


def transform(ctx: HandlerContext) -> dict[str, Any]:
    ctx.apply_faults()
    source = str(ctx.job.params["source"])
    dest = str(ctx.job.params["dest"])
    dead_letter = str(ctx.job.params["dead_letter"])
    accepted = rejected = 0
    for row in ctx.workspace.list(source):
        ctx.check_deadline()
        errors = validate_record(row)
        record_id = str(row.get("id") or "")
        if errors:
            poisoned = dict(row)
            poisoned["status"] = "failed"
            if not str(poisoned.get("id") or "").strip():
                poisoned["id"] = f"REC-BAD-{rejected + 1:04d}"
            ctx.workspace.upsert(dead_letter, poisoned, dry_run=ctx.dry_run)
            rejected += 1
            ctx.logger.log("record_rejected", record_id=record_id, errors=errors[:5])
            continue
        clean = dict(row)
        clean["status"] = "processed"
        ctx.workspace.upsert(dest, clean, dry_run=ctx.dry_run)
        accepted += 1
    ctx.logger.log("transform_done", job_id=ctx.job.id, accepted=accepted, rejected=rejected)
    return {"accepted": accepted, "rejected": rejected}


def export(ctx: HandlerContext) -> dict[str, Any]:
    ctx.apply_faults()
    source = str(ctx.job.params["source"])
    dest = str(ctx.job.params["dest"])
    template = str(ctx.job.params["name_template"])
    records = ctx.workspace.list(source)
    name = render_name(
        template,
        window=ctx.window_start_ms,
        pipeline=ctx.pipeline,
        run_id=ctx.run_id.replace(":", "_"),
    )
    payload = {
        "id": name,
        "pipeline": ctx.pipeline,
        "window_start_ms": ctx.window_start_ms,
        "run_id": ctx.run_id,
        "clean_count": len(records),
        "record_ids": [str(row["id"]) for row in records],
        "dead_letter_count": len(ctx.workspace.list("dead_letter")),
    }
    outcome = ctx.workspace.put_blob(dest, name, payload, dry_run=ctx.dry_run)
    ctx.logger.log("export_done", job_id=ctx.job.id, name=name, outcome=outcome)
    return {"name": name, "clean_count": payload["clean_count"], "outcome": outcome}


def notify(ctx: HandlerContext) -> dict[str, Any]:
    ctx.apply_faults()
    channel = str(ctx.job.params["channel"])
    export_bucket = str(ctx.job.params["export_bucket"])
    template = str(ctx.job.params.get("name_template") or "report-{window}.json")
    name = render_name(
        template,
        window=ctx.window_start_ms,
        pipeline=ctx.pipeline,
        run_id=ctx.run_id.replace(":", "_"),
    )
    export_blob = ctx.workspace.get(export_bucket, name)
    if export_blob is None:
        raise ValidationError(f"export blob {name} is missing")
    note_id = f"NTF-{ctx.pipeline}-w{ctx.window_start_ms}"
    payload = {
        "id": note_id,
        "channel": channel,
        "export_name": name,
        "clean_count": export_blob.get("clean_count", 0),
        "run_id": ctx.run_id,
        "window_start_ms": ctx.window_start_ms,
    }
    outcome = ctx.workspace.put_blob("notifications", note_id, payload, dry_run=ctx.dry_run)
    ctx.logger.log("notify_done", job_id=ctx.job.id, note_id=note_id, outcome=outcome)
    return {"id": note_id, "export_name": name, "outcome": outcome}


def cleanup(ctx: HandlerContext) -> dict[str, Any]:
    ctx.apply_faults()
    inbox = str(ctx.job.params["inbox"])
    clean = str(ctx.job.params["clean"])
    archive = str(ctx.job.params["archive"])
    clean_ids = {str(row["id"]) for row in ctx.workspace.list(clean)}
    moved = 0
    for row in ctx.workspace.list(inbox):
        ctx.check_deadline()
        record_id = str(row["id"])
        if record_id not in clean_ids:
            continue
        archived = dict(row)
        archived["status"] = "processed"
        ctx.workspace.upsert(archive, archived, dry_run=ctx.dry_run)
        ctx.workspace.delete(inbox, record_id, dry_run=ctx.dry_run)
        moved += 1
    ctx.logger.log("cleanup_done", job_id=ctx.job.id, moved=moved)
    return {"moved": moved}


def heartbeat(ctx: HandlerContext) -> dict[str, Any]:
    ctx.apply_faults()
    beat_id = f"HB-{ctx.window_start_ms}"
    payload = {
        "id": beat_id,
        "run_id": ctx.run_id,
        "at_ms": ctx.clock.now_ms(),
        "window_start_ms": ctx.window_start_ms,
    }
    outcome = ctx.workspace.put_blob("heartbeats", beat_id, payload, dry_run=ctx.dry_run)
    ctx.logger.log("heartbeat_done", job_id=ctx.job.id, beat_id=beat_id)
    return {"id": beat_id, "outcome": outcome, "at_ms": payload["at_ms"]}


REGISTRY: dict[str, Callable[[HandlerContext], dict[str, Any]]] = {
    "ingest": ingest,
    "transform": transform,
    "export": export,
    "notify": notify,
    "cleanup": cleanup,
    "heartbeat": heartbeat,
}


def dispatch(name: str, ctx: HandlerContext) -> dict[str, Any]:
    fn = REGISTRY.get(name)
    if fn is None:
        raise PermanentError("unknown_handler", f"unknown handler {name}")
    return fn(ctx)
