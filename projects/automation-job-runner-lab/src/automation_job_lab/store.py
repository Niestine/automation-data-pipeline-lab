"""In-memory and durable workspace buckets for records and blobs."""

from __future__ import annotations

from typing import Any, Optional
import copy
import json
from pathlib import Path

from .errors import StateError, ValidationError
from .models import BLOB_BUCKETS, RECORD_BUCKETS
from .persist import atomic_write_json, read_json


class Workspace:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.records: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in RECORD_BUCKETS}
        self.blobs: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in BLOB_BUCKETS}
        self._staged_records: dict[str, dict[str, dict[str, Any]]] = {
            name: {} for name in RECORD_BUCKETS
        }
        self._staged_blobs: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in BLOB_BUCKETS}
        self._staged_deletes: set[tuple[str, str]] = set()
        if self.path is not None and self.path.exists():
            self._load()

    def discard_staged(self) -> None:
        for name in RECORD_BUCKETS:
            self._staged_records[name].clear()
        for name in BLOB_BUCKETS:
            self._staged_blobs[name].clear()
        self._staged_deletes.clear()

    def counts(self) -> dict[str, int]:
        out = {name: len(self.list(name)) for name in RECORD_BUCKETS}
        for name in BLOB_BUCKETS:
            out[name] = len(self.list(name))
        return out

    def list(self, bucket: str) -> list[dict[str, Any]]:
        data = self._merged(bucket)
        items = [copy.deepcopy(item) for item in data.values()]
        items.sort(key=lambda row: str(row.get("id") or row.get("name") or ""))
        return items

    def get(self, bucket: str, item_id: str) -> Optional[dict[str, Any]]:
        data = self._merged(bucket)
        item = data.get(item_id)
        return copy.deepcopy(item) if item is not None else None

    def upsert(self, bucket: str, record: dict[str, Any], *, dry_run: bool = False) -> str:
        if bucket not in RECORD_BUCKETS:
            raise ValidationError(f"unknown record bucket {bucket}")
        if not isinstance(record, dict):
            raise ValidationError("record must be an object")
        item_id = str(record.get("id") or "").strip()
        if not item_id:
            raise ValidationError("record.id is required")
        incoming = copy.deepcopy(record)
        incoming["id"] = item_id
        existing = self.get(bucket, item_id)
        outcome = _version_outcome(existing, incoming)
        if dry_run:
            if outcome in {"inserted", "updated"}:
                self._staged_deletes.discard((bucket, item_id))
                self._staged_records[bucket][item_id] = incoming
            return outcome
        if outcome in {"inserted", "updated"}:
            self.records[bucket][item_id] = incoming
            self._flush()
        return outcome

    def delete(self, bucket: str, item_id: str, *, dry_run: bool = False) -> bool:
        if bucket not in RECORD_BUCKETS:
            raise ValidationError(f"unknown record bucket {bucket}")
        if dry_run:
            present = self.get(bucket, item_id) is not None
            self._staged_deletes.add((bucket, item_id))
            self._staged_records[bucket].pop(item_id, None)
            return present
        present = item_id in self.records[bucket]
        self.records[bucket].pop(item_id, None)
        if present:
            self._flush()
        return present

    def put_blob(self, bucket: str, name: str, payload: dict[str, Any], *, dry_run: bool = False) -> str:
        if bucket not in BLOB_BUCKETS:
            raise ValidationError(f"unknown blob bucket {bucket}")
        if not name or not isinstance(name, str):
            raise ValidationError("blob name is required")
        stored = copy.deepcopy(payload)
        stored.setdefault("id", name)
        if dry_run:
            self._staged_deletes.discard((bucket, name))
            self._staged_blobs[bucket][name] = stored
            return "staged"
        existed = name in self.blobs[bucket]
        self.blobs[bucket][name] = stored
        self._flush()
        return "updated" if existed else "inserted"

    def _merged(self, bucket: str) -> dict[str, dict[str, Any]]:
        if bucket in RECORD_BUCKETS:
            data = dict(self.records[bucket])
            data.update(self._staged_records[bucket])
        elif bucket in BLOB_BUCKETS:
            data = dict(self.blobs[bucket])
            data.update(self._staged_blobs[bucket])
        else:
            raise ValidationError(f"unknown bucket {bucket}")
        for item_id in list(data):
            if (bucket, item_id) in self._staged_deletes:
                data.pop(item_id, None)
        return data

    def _flush(self) -> None:
        if self.path is None:
            return
        payload = {
            "records": self.records,
            "blobs": self.blobs,
        }
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:
            raise StateError(f"could not write workspace {self.path}: {exc}") from exc

    def _load(self) -> None:
        assert self.path is not None
        try:
            payload = read_json(self.path)
            if not isinstance(payload, dict):
                raise ValueError("workspace must be an object")
            records = payload.get("records") or {}
            blobs = payload.get("blobs") or {}
            if not isinstance(records, dict) or not isinstance(blobs, dict):
                raise ValueError("workspace.records and workspace.blobs must be objects")
            for name in RECORD_BUCKETS:
                bucket = records.get(name) or {}
                if not isinstance(bucket, dict):
                    raise ValueError(f"workspace.records.{name} must be an object")
                self.records[name] = {str(key): dict(value) for key, value in bucket.items()}
            for name in BLOB_BUCKETS:
                bucket = blobs.get(name) or {}
                if not isinstance(bucket, dict):
                    raise ValueError(f"workspace.blobs.{name} must be an object")
                self.blobs[name] = {str(key): dict(value) for key, value in bucket.items()}
        except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise StateError(f"corrupt workspace {self.path}: {exc}") from exc


def _version_outcome(existing: Optional[dict[str, Any]], incoming: dict[str, Any]) -> str:
    if existing is None:
        return "inserted"
    existing_version = existing.get("version", 1)
    incoming_version = incoming.get("version", 1)
    if type(existing_version) is int and type(incoming_version) is int:
        if incoming_version < existing_version:
            return "ignored_stale"
        if incoming_version == existing_version:
            if existing == incoming:
                return "ignored_duplicate"
            return "ignored_conflict"
        return "updated"
    if existing == incoming:
        return "ignored_duplicate"
    return "updated"
