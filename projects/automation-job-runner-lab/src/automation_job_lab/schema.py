"""JSON contracts for catalogs, jobs, and inbox records.

Python's json module accepts NaN/Infinity; parse_json_text rejects those constants.
"""

from __future__ import annotations

from typing import Any
import json
import math
import re

from .errors import SchemaError
from .models import (
    BLOB_BUCKETS,
    CURRENCIES,
    HANDLERS,
    IDEMPOTENCY_MODES,
    ID_PATTERN,
    ISO_Z,
    RECORD_BUCKETS,
    RECORD_ID_PATTERN,
    RECORD_KINDS,
    RECORD_STATUSES,
    TICKET_QUEUES,
    Catalog,
    JobSpec,
)


RECORD_BUCKET = {"type": "string", "enum": list(RECORD_BUCKETS)}
BLOB_BUCKET = {"type": "string", "enum": list(BLOB_BUCKETS)}

RETRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "max_attempts",
        "base_delay_ms",
        "max_delay_ms",
        "multiplier",
        "jitter_ms",
    ],
    "properties": {
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
        "base_delay_ms": {"type": "integer", "minimum": 0, "maximum": 60_000},
        "max_delay_ms": {"type": "integer", "minimum": 0, "maximum": 60_000},
        "multiplier": {"type": "number", "minimum": 1, "maximum": 10},
        "jitter_ms": {"type": "integer", "minimum": 0, "maximum": 1_000},
    },
}

SCHEDULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "every_ms": {"type": "integer", "minimum": 1_000, "maximum": 86_400_000},
        "offset_ms": {"type": "integer", "minimum": 0, "maximum": 86_400_000},
        "cron_minute": {"type": "integer", "minimum": 0, "maximum": 59},
        "cron_hour": {"type": "integer", "minimum": 0, "maximum": 23},
    },
}

IDEMPOTENCY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mode"],
    "properties": {
        "mode": {"type": "string", "enum": list(IDEMPOTENCY_MODES)},
    },
}

JOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "handler",
        "schedule",
        "retry",
        "timeout_ms",
        "depends_on",
        "idempotency",
        "params",
    ],
    "properties": {
        "id": {"type": "string", "pattern": ID_PATTERN},
        "handler": {"type": "string", "enum": list(HANDLERS)},
        "schedule": SCHEDULE_SCHEMA,
        "retry": RETRY_SCHEMA,
        "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 60_000},
        "depends_on": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "idempotency": IDEMPOTENCY_SCHEMA,
        "params": {"type": "object"},
    },
}

CATALOG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pipeline", "window_ms", "jobs"],
    "properties": {
        "pipeline": {"type": "string", "pattern": ID_PATTERN},
        "window_ms": {"type": "integer", "minimum": 1_000, "maximum": 86_400_000},
        "jobs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": JOB_SCHEMA,
        },
    },
}

RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "status", "updated_at", "version", "payload"],
    "properties": {
        "id": {"type": "string", "pattern": RECORD_ID_PATTERN},
        "kind": {"type": "string", "enum": list(RECORD_KINDS)},
        "status": {"type": "string", "enum": list(RECORD_STATUSES)},
        "amount_cents": {"type": "integer", "minimum": 0, "maximum": 100_000_000},
        "updated_at": {"type": "string", "pattern": ISO_Z},
        "version": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        "payload": {"type": "object"},
    },
}

LINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["desc", "cents"],
    "properties": {
        "desc": {"type": "string", "minLength": 1, "maxLength": 80},
        "cents": {"type": "integer", "minimum": 0, "maximum": 100_000_000},
    },
}

HANDLER_PARAM_SCHEMAS: dict[str, dict[str, Any]] = {
    "ingest": {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "dest"],
        "properties": {
            "source": RECORD_BUCKET,
            "dest": RECORD_BUCKET,
        },
    },
    "transform": {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "dest", "dead_letter"],
        "properties": {
            "source": RECORD_BUCKET,
            "dest": RECORD_BUCKET,
            "dead_letter": RECORD_BUCKET,
        },
    },
    "export": {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "dest", "name_template"],
        "properties": {
            "source": RECORD_BUCKET,
            "dest": BLOB_BUCKET,
            "name_template": {"type": "string", "minLength": 1, "maxLength": 80},
        },
    },
    "notify": {
        "type": "object",
        "additionalProperties": False,
        "required": ["channel", "export_bucket"],
        "properties": {
            "channel": {"type": "string", "minLength": 1, "maxLength": 32},
            "export_bucket": BLOB_BUCKET,
            "name_template": {"type": "string", "minLength": 1, "maxLength": 80},
        },
    },
    "cleanup": {
        "type": "object",
        "additionalProperties": False,
        "required": ["inbox", "clean", "archive"],
        "properties": {
            "inbox": RECORD_BUCKET,
            "clean": RECORD_BUCKET,
            "archive": RECORD_BUCKET,
        },
    },
    "heartbeat": {
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    },
}


def _reject_constant(name: str) -> Any:
    raise SchemaError(f"non-finite JSON number {name}")


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"invalid JSON: {exc}") from exc


def check_schema(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not _matches_type(value, allowed):
            errors.append(f"{path}: expected {types}, got {type(value).__name__}")
            return errors
    if value is None:
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not an allowed value")
    if "pattern" in schema and isinstance(value, str):
        if re.fullmatch(schema["pattern"], value) is None:
            errors.append(f"{path}: does not match pattern")
    if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than minLength")
    if "maxLength" in schema and isinstance(value, str) and len(value) > schema["maxLength"]:
        errors.append(f"{path}: longer than maxLength")
    if "minimum" in schema and _is_number(value) and value < schema["minimum"]:
        errors.append(f"{path}: below minimum")
    if "maximum" in schema and _is_number(value) and value > schema["maximum"]:
        errors.append(f"{path}: above maximum")
    if isinstance(value, dict) and (
        schema.get("type") == "object" or "properties" in schema or "additionalProperties" in schema
    ):
        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        if additional is False:
            extra = sorted(set(value) - set(props))
            for key in extra:
                errors.append(f"{path}.{key}: additional property")
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}: required")
        for key, sub in props.items():
            if key in value:
                errors.extend(check_schema(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list) and schema.get("type") == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(check_schema(item, item_schema, f"{path}[{index}]"))
    return errors


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and not isinstance(value, bool)


def _matches_type(value: Any, allowed: list[str]) -> bool:
    for expected in allowed:
        if expected == "null" and value is None:
            return True
        if expected == "object" and isinstance(value, dict):
            return True
        if expected == "array" and isinstance(value, list):
            return True
        if expected == "string" and type(value) is str:
            return True
        if expected == "integer" and type(value) is int:
            return True
        if expected == "boolean" and type(value) is bool:
            return True
        if expected == "number" and _is_number(value):
            if isinstance(value, float) and not math.isfinite(value):
                return False
            return True
    return False


def load_catalog(data: Any) -> Catalog:
    errors = check_schema(data, CATALOG_SCHEMA, "$")
    if errors:
        raise SchemaError("; ".join(errors), errors)
    jobs_data = data["jobs"]
    ids = [item["id"] for item in jobs_data]
    if len(ids) != len(set(ids)):
        raise SchemaError("duplicate job id", ["duplicate job id"])
    id_set = set(ids)
    jobs: list[JobSpec] = []
    for index, item in enumerate(jobs_data):
        param_errors = check_schema(
            item.get("params") or {},
            HANDLER_PARAM_SCHEMAS[item["handler"]],
            f"$.jobs[{index}].params",
        )
        if param_errors:
            raise SchemaError("; ".join(param_errors), param_errors)
        job = JobSpec.from_validated(item)
        for dep in job.depends_on:
            if dep == job.id:
                raise SchemaError(
                    f"job {job.id} depends on itself",
                    [f"job {job.id} depends on itself"],
                )
            if dep not in id_set:
                raise SchemaError(
                    f"job {job.id} depends on unknown {dep}",
                    [f"job {job.id} depends on unknown {dep}"],
                )
        jobs.append(job)
    catalog = Catalog(pipeline=str(data["pipeline"]), window_ms=int(data["window_ms"]), jobs=tuple(jobs))
    for job in catalog.jobs:
        _check_interval(job, catalog.window_ms)
    topo_sort(catalog.jobs)
    return catalog


def _check_interval(job: JobSpec, window_ms: int) -> None:
    spec = job.schedule
    problem = None
    if spec.every_ms is None:
        if spec.offset_ms:
            problem = "offset_ms requires every_ms"
    elif spec.every_ms % window_ms:
        problem = "every_ms must be a multiple of window_ms"
    elif spec.offset_ms % window_ms or spec.offset_ms >= spec.every_ms:
        problem = "offset_ms must be a multiple of window_ms below every_ms"
    if problem is not None:
        message = f"job {job.id}: {problem}"
        raise SchemaError(message, [message])


def topo_sort(jobs: tuple[JobSpec, ...] | list[JobSpec]) -> list[JobSpec]:
    by_id = {job.id: job for job in jobs}
    indegree = {job.id: 0 for job in jobs}
    children: dict[str, list[str]] = {job.id: [] for job in jobs}
    for job in jobs:
        for dep in job.depends_on:
            if dep not in by_id:
                raise SchemaError(
                    f"job {job.id} depends on unknown {dep}",
                    [f"job {job.id} depends on unknown {dep}"],
                )
            indegree[job.id] += 1
            children[dep].append(job.id)
    ready = sorted(job_id for job_id, degree in indegree.items() if degree == 0)
    ordered: list[JobSpec] = []
    while ready:
        job_id = ready.pop(0)
        ordered.append(by_id[job_id])
        nxt: list[str] = []
        for child in children[job_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                nxt.append(child)
        ready = sorted(ready + nxt)
    if len(ordered) != len(by_id):
        raise SchemaError("job graph contains a cycle", ["job graph contains a cycle"])
    return ordered


def validate_record(row: Any) -> list[str]:
    errors = check_schema(row, RECORD_SCHEMA, "$")
    if errors:
        return errors
    kind = row["kind"]
    payload = row["payload"]
    if kind == "invoice":
        errors.extend(_validate_invoice(row, payload))
    elif kind == "ticket":
        errors.extend(_validate_ticket(payload))
    elif kind == "file_index":
        errors.extend(_validate_file_index(payload))
    elif kind == "report":
        errors.extend(_validate_report(payload))
    return errors


def _validate_invoice(row: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "amount_cents" not in row:
        return ["$.amount_cents: required for invoice"]
    extra = sorted(set(payload) - {"lines", "currency"})
    for key in extra:
        errors.append(f"$.payload.{key}: additional property")
    if "currency" not in payload:
        errors.append("$.payload.currency: required")
    elif payload["currency"] not in CURRENCIES:
        errors.append("$.payload.currency: invalid enum")
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        errors.append("$.payload.lines: expected a non-empty array")
        return errors
    if len(lines) > 50:
        errors.append("$.payload.lines: more than maxItems")
    total = 0
    for index, line in enumerate(lines):
        errors.extend(check_schema(line, LINE_SCHEMA, f"$.payload.lines[{index}]"))
        if isinstance(line, dict) and type(line.get("cents")) is int:
            total += line["cents"]
    if type(row.get("amount_cents")) is int and total != row["amount_cents"]:
        errors.append("$.amount_cents: does not match sum of payload.lines")
    return errors


def _validate_ticket(payload: dict[str, Any]) -> list[str]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["queue", "priority", "summary"],
        "properties": {
            "queue": {"type": "string", "enum": list(TICKET_QUEUES)},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5},
            "summary": {"type": "string", "minLength": 1, "maxLength": 120},
        },
    }
    return check_schema(payload, schema, "$.payload")


def _validate_file_index(payload: dict[str, Any]) -> list[str]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "bytes"],
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 120, "pattern": r"^[a-z0-9_./-]+$"},
            "bytes": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
        },
    }
    errors = check_schema(payload, schema, "$.payload")
    path = payload.get("path")
    if isinstance(path, str) and ".." in path:
        errors.append("$.payload.path: parent traversal is not allowed")
    return errors


def _validate_report(payload: dict[str, Any]) -> list[str]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "pages"],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 80},
            "pages": {"type": "integer", "minimum": 1, "maximum": 500},
        },
    }
    return check_schema(payload, schema, "$.payload")
