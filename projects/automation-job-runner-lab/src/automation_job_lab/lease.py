"""Single-host job leases with expiry."""

from __future__ import annotations

from typing import Optional
import json
from pathlib import Path

from .errors import StateError
from .models import Lease
from .persist import atomic_write_json, read_json


class LeaseStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._leases: dict[str, Lease] = {}
        if self.path is not None and self.path.exists():
            self._load()

    def get(self, job_id: str) -> Optional[Lease]:
        item = self._leases.get(job_id)
        if item is None:
            return None
        return Lease.from_dict(item.to_dict())

    def acquire(self, job_id: str, holder: str, now_ms: int, ttl_ms: int) -> bool:
        existing = self._leases.get(job_id)
        if existing is not None and existing.expires_ms > now_ms and existing.holder != holder:
            return False
        self._leases[job_id] = Lease(
            job_id=job_id,
            holder=holder,
            expires_ms=int(now_ms) + max(1, int(ttl_ms)),
        )
        self._flush()
        return True

    def release(self, job_id: str, holder: str) -> bool:
        existing = self._leases.get(job_id)
        if existing is None or existing.holder != holder:
            return False
        self._leases.pop(job_id, None)
        self._flush()
        return True

    def _flush(self) -> None:
        if self.path is None:
            return
        payload = {"leases": {key: item.to_dict() for key, item in self._leases.items()}}
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:
            raise StateError(f"could not write leases {self.path}: {exc}") from exc

    def _load(self) -> None:
        assert self.path is not None
        try:
            payload = read_json(self.path)
            leases = payload.get("leases") if isinstance(payload, dict) else None
            if not isinstance(leases, dict):
                raise ValueError("leases must be an object")
            loaded: dict[str, Lease] = {}
            for key, item in leases.items():
                loaded[str(key)] = Lease.from_dict(item)
            self._leases = loaded
        except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise StateError(f"corrupt leases {self.path}: {exc}") from exc
