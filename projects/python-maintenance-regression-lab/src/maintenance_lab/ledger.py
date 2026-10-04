"""Idempotency ledger keyed by ISO week and feed sha256."""

from __future__ import annotations

from typing import Any, Optional
import json
from pathlib import Path

from .errors import StateError
from .persist import atomic_write_json, read_json


class FeedLedger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._rows: dict[str, dict[str, Any]] = {}
        self._dry = False
        self._backup: Optional[dict[str, dict[str, Any]]] = None
        if self.path is not None and self.path.exists():
            self._rows = _load_path(self.path)

    def __len__(self) -> int:
        return len(self._rows)

    def begin_dry_run(self) -> None:
        if self._dry:
            return
        self._backup = {key: dict(value) for key, value in self._rows.items()}
        self._dry = True

    def abort_dry_run(self) -> None:
        if not self._dry:
            return
        if self._backup is not None:
            self._rows = self._backup
        self._backup = None
        self._dry = False

    def key(self, week_id: str, sha256: str) -> str:
        return f"{week_id}:{sha256}"

    def get(self, week_id: str, sha256: str) -> Optional[dict[str, Any]]:
        row = self._rows.get(self.key(week_id, sha256))
        if row is None:
            return None
        return dict(row)

    def record(self, week_id: str, sha256: str, payload: dict[str, Any]) -> None:
        self._rows[self.key(week_id, sha256)] = dict(payload)
        self._persist()

    def _persist(self) -> None:
        if self._dry or self.path is None:
            return
        try:
            atomic_write_json(self.path, {"entries": self._rows})
        except OSError as exc:
            raise StateError(f"could not write ledger {self.path}: {exc}") from exc


def _load_path(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StateError(f"corrupt ledger {path}: {exc}") from exc
    if not isinstance(data, dict) or "entries" not in data:
        raise StateError(f"corrupt ledger {path}: missing entries")
    entries = data["entries"]
    if not isinstance(entries, dict):
        raise StateError(f"corrupt ledger {path}: entries must be an object")
    loaded: dict[str, dict[str, Any]] = {}
    for key, value in entries.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise StateError(f"corrupt ledger {path}: bad entry")
        loaded[key] = dict(value)
    return loaded
