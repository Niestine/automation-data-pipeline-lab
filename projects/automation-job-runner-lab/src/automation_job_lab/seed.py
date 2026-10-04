"""Deterministic synthetic catalog, inbox, and fault script."""

from __future__ import annotations

from typing import Any


DEFAULT_RETRY: dict[str, Any] = {
    "max_attempts": 4,
    "base_delay_ms": 10,
    "max_delay_ms": 200,
    "multiplier": 2.0,
    "jitter_ms": 3,
}

WINDOW_MS = 3_600_000


def build_catalog() -> dict[str, Any]:
    return {
        "pipeline": "hourly-ops",
        "window_ms": WINDOW_MS,
        "jobs": [
            {
                "id": "ingest-inbox",
                "handler": "ingest",
                "schedule": {"every_ms": WINDOW_MS, "offset_ms": 0},
                "retry": dict(DEFAULT_RETRY),
                "timeout_ms": 5000,
                "depends_on": [],
                "idempotency": {"mode": "window"},
                "params": {"source": "inbox", "dest": "staging"},
            },
            {
                "id": "transform-records",
                "handler": "transform",
                "schedule": {},
                "retry": dict(DEFAULT_RETRY),
                "timeout_ms": 5000,
                "depends_on": ["ingest-inbox"],
                "idempotency": {"mode": "input_hash"},
                "params": {
                    "source": "staging",
                    "dest": "clean",
                    "dead_letter": "dead_letter",
                },
            },
            {
                "id": "export-report",
                "handler": "export",
                "schedule": {},
                "retry": dict(DEFAULT_RETRY),
                "timeout_ms": 5000,
                "depends_on": ["transform-records"],
                "idempotency": {"mode": "window"},
                "params": {
                    "source": "clean",
                    "dest": "exports",
                    "name_template": "report-{window}.json",
                },
            },
            {
                "id": "notify-ops",
                "handler": "notify",
                "schedule": {},
                "retry": dict(DEFAULT_RETRY),
                "timeout_ms": 5000,
                "depends_on": ["export-report"],
                "idempotency": {"mode": "run"},
                "params": {
                    "channel": "ops",
                    "export_bucket": "exports",
                    "name_template": "report-{window}.json",
                },
            },
            {
                "id": "cleanup-inbox",
                "handler": "cleanup",
                "schedule": {},
                "retry": dict(DEFAULT_RETRY),
                "timeout_ms": 5000,
                "depends_on": ["export-report"],
                "idempotency": {"mode": "window"},
                "params": {"inbox": "inbox", "clean": "clean", "archive": "archive"},
            },
            {
                "id": "heartbeat-log",
                "handler": "heartbeat",
                "schedule": {"cron_minute": 0},
                "retry": dict(DEFAULT_RETRY),
                "timeout_ms": 1000,
                "depends_on": [],
                "idempotency": {"mode": "window"},
                "params": {},
            },
        ],
    }


def _invoice(index: int, record_id: str, updated_at: str) -> dict[str, Any]:
    lines = [
        {"desc": f"line-{index}-a", "cents": 1000 * index},
        {"desc": f"line-{index}-b", "cents": 250},
    ]
    amount = sum(item["cents"] for item in lines)
    return {
        "id": record_id,
        "kind": "invoice",
        "status": "pending",
        "amount_cents": amount,
        "updated_at": updated_at,
        "version": 1,
        "payload": {"lines": lines, "currency": "USD"},
    }


def _ticket(index: int, record_id: str, updated_at: str) -> dict[str, Any]:
    queues = ("ops", "support", "billing")
    return {
        "id": record_id,
        "kind": "ticket",
        "status": "pending",
        "updated_at": updated_at,
        "version": 1,
        "payload": {
            "queue": queues[(index - 1) % 3],
            "priority": 1 + (index % 5),
            "summary": f"synthetic ticket {index}",
        },
    }


def _file_index(index: int, record_id: str, updated_at: str) -> dict[str, Any]:
    return {
        "id": record_id,
        "kind": "file_index",
        "status": "ready",
        "updated_at": updated_at,
        "version": 1,
        "payload": {"path": f"inbox/file-{index:02d}.txt", "bytes": 100 * index},
    }


def _report(index: int, record_id: str, updated_at: str) -> dict[str, Any]:
    return {
        "id": record_id,
        "kind": "report",
        "status": "ready",
        "updated_at": updated_at,
        "version": 1,
        "payload": {"title": f"Weekly {index}", "pages": 1 + (index % 3)},
    }


def _valid_record(index: int, record_id: str, updated_at: str) -> dict[str, Any]:
    kind = (index - 1) % 4
    if kind == 0:
        return _invoice(index, record_id, updated_at)
    if kind == 1:
        return _ticket(index, record_id, updated_at)
    if kind == 2:
        return _file_index(index, record_id, updated_at)
    return _report(index, record_id, updated_at)


def build_inbox(*, window: int = 0) -> list[dict[str, Any]]:
    hour = window
    if window == 0:
        rows = [
            _valid_record(i, f"REC-{1000 + i}", f"2026-01-01T{hour:02d}:00:{i:02d}Z")
            for i in range(1, 11)
        ]
        extra = dict(_ticket(98, "REC-1098", "2026-01-01T00:00:58Z"))
        extra["internal_note"] = "poison-extra-field"
        mismatch = _invoice(99, "REC-1099", "2026-01-01T00:00:59Z")
        mismatch["amount_cents"] = 99_999
        rows.append(extra)
        rows.append(mismatch)
        return rows
    base = 1000 * (window + 1)
    return [
        _valid_record(
            window * 10 + i,
            f"REC-{base + i}",
            f"2026-01-01T{hour:02d}:00:{i:02d}Z",
        )
        for i in range(1, 4)
    ]


def build_fault_script() -> list[dict[str, Any]]:
    return [
        {"job_id": "ingest-inbox", "attempt": 1, "error": "timeout"},
        {"job_id": "transform-records", "attempt": 1, "error": "transient_io"},
    ]
