"""Idempotency ledger of job executions."""

from __future__ import annotations

from typing import Optional
import json
from pathlib import Path

from .errors import StateError
from .models import ExecutionRecord
from .persist import atomic_write_json, read_json


class ExecutionLedger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._entries: dict[str, ExecutionRecord] = {}
        self._staged: dict[str, ExecutionRecord] = {}
        if self.path is not None and self.path.exists():
            self._load()

    def __len__(self) -> int:
        return len(self._entries)

    def discard_staged(self) -> None:
        self._staged.clear()

    def get(self, key: str, *, dry_run: bool = False) -> Optional[ExecutionRecord]:
        if dry_run and key in self._staged:
            return ExecutionRecord.from_dict(self._staged[key].to_dict())
        item = self._entries.get(key)
        if item is None:
            return None
        return ExecutionRecord.from_dict(item.to_dict())

    def for_job(self, job_id: str) -> list[ExecutionRecord]:
        return [
            ExecutionRecord.from_dict(item.to_dict())
            for item in self._entries.values()
            if item.job_id == job_id
        ]

    def put(self, record: ExecutionRecord, *, dry_run: bool = False) -> None:
        stored = ExecutionRecord.from_dict(record.to_dict())
        if dry_run:
            self._staged[stored.key] = stored
            return
        self._entries[stored.key] = stored
        self._flush()

    def _flush(self) -> None:
        if self.path is None:
            return
        payload = {"entries": {key: item.to_dict() for key, item in self._entries.items()}}
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:
            raise StateError(f"could not write ledger {self.path}: {exc}") from exc

    def _load(self) -> None:
        assert self.path is not None
        try:
            payload = read_json(self.path)
            entries = payload.get("entries") if isinstance(payload, dict) else None
            if not isinstance(entries, dict):
                raise ValueError("ledger.entries must be an object")
            loaded: dict[str, ExecutionRecord] = {}
            for key, item in entries.items():
                record = ExecutionRecord.from_dict(item)
                loaded[str(key)] = record
            self._entries = loaded
        except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise StateError(f"corrupt ledger {self.path}: {exc}") from exc
