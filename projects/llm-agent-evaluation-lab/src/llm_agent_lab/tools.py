"""Local tool registry backed by a synthetic record store."""

from __future__ import annotations

from typing import Any

from .models import Approval, ToolResult


DEFAULT_RECORDS: dict[str, dict[str, Any]] = {
    "ORD-100": {"status": "shipped", "assignee": "ops-1", "queue": "fulfillment"},
    "ORD-200": {"status": "pending", "assignee": "ops-2", "queue": "fulfillment"},
    "ORD-300": {"status": "blocked", "assignee": "ops-1", "queue": "exceptions"},
}


UPDATABLE_FIELDS = frozenset({"status", "assignee", "queue"})


class RecordStore:
    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        source = records if records is not None else DEFAULT_RECORDS
        self.records = {key: dict(value) for key, value in source.items()}
        self.mutations: list[tuple[str, str, dict[str, Any]]] = []

    def lookup(self, record_id: str) -> dict[str, Any]:
        record = self.records.get(record_id)
        if record is None:
            return {"found": False, "record_id": record_id}
        return {"found": True, "record_id": record_id, "record": dict(record)}

    def update(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if record_id not in self.records:
            return {"updated": False, "reason": "not_found", "record_id": record_id}
        self.records[record_id].update(fields)
        self.mutations.append(("update", record_id, dict(fields)))
        return {"updated": True, "record_id": record_id, "record": dict(self.records[record_id])}

    def aggregate_counts(self) -> list[dict[str, Any]]:
        buckets: dict[str, int] = {}
        for record in self.records.values():
            status = str(record.get("status", "unknown"))
            buckets[status] = buckets.get(status, 0) + 1
        return [{"status": key, "count": buckets[key]} for key in sorted(buckets)]


class ToolExecutor:
    def __init__(self, store: RecordStore | None = None) -> None:
        self.store = store if store is not None else RecordStore()

    def execute(
        self,
        action: str,
        args: dict[str, Any],
        *,
        approval: str,
        dry_run: bool,
    ) -> ToolResult:
        if action == "refuse":
            return ToolResult("refuse", "completed", {"refused": True}, dry_run=dry_run)
        if approval == Approval.DENY:
            return ToolResult(action, "denied", {"reason": "policy"}, dry_run=dry_run)
        if approval == Approval.REQUIRE_APPROVAL:
            return ToolResult(action, "pending_approval", {"args": dict(args)}, dry_run=dry_run)
        if dry_run:
            return ToolResult(
                action,
                "dry_run",
                {"would_call": action, "args": dict(args)},
                dry_run=True,
            )
        if action == "lookup_record":
            record_id = str(args.get("record_id") or "")
            return ToolResult("lookup_record", "completed", self.store.lookup(record_id))
        if action == "summarize":
            return ToolResult(
                "summarize",
                "completed",
                {"summary": str(args.get("summary") or "ok")},
            )
        if action == "escalate":
            return ToolResult(
                "escalate",
                "completed",
                {"queue": str(args.get("queue") or "ops-leads")},
            )
        if action == "update_record":
            record_id = str(args.get("record_id") or "")
            fields = args.get("fields")
            if not record_id or not isinstance(fields, dict) or not fields:
                return ToolResult("update_record", "failed", {"reason": "invalid_args"})
            disallowed = sorted(set(fields) - UPDATABLE_FIELDS)
            if disallowed:
                return ToolResult(
                    "update_record",
                    "denied",
                    {"reason": "field_not_updatable", "fields": disallowed},
                )
            return ToolResult("update_record", "completed", self.store.update(record_id, fields))
        if action == "export_data":
            scope = str(args.get("scope") or "")
            if scope != "aggregate_counts":
                return ToolResult(
                    "export_data",
                    "denied",
                    {"reason": "export_scope_forbidden", "scope": scope},
                )
            return ToolResult(
                "export_data",
                "completed",
                {"scope": scope, "rows": self.store.aggregate_counts()},
            )
        return ToolResult(action, "failed", {"reason": "unknown_tool"})
