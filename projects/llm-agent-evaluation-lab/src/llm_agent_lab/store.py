"""Idempotent run store keyed by ticket, contract, input hash, and mode."""

from __future__ import annotations

from typing import Optional
import copy
import hashlib

from .models import CONTRACT_VERSION, RunResult, Ticket


class RunStore:
    def __init__(self) -> None:
        self._data: dict[str, RunResult] = {}

    def key(self, ticket: Ticket, *, dry_run: bool, approve: bool) -> str:
        mode = "dry" if dry_run else "live"
        gate = "approved" if approve else "auto"
        return f"{ticket.ticket_id}:{CONTRACT_VERSION}:{ticket.input_hash()}:{mode}:{gate}"

    def run_id(self, ticket: Ticket, *, dry_run: bool, approve: bool) -> str:
        material = self.key(ticket, dry_run=dry_run, approve=approve).encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:12]

    def get(self, ticket: Ticket, *, dry_run: bool, approve: bool) -> Optional[RunResult]:
        hit = self._data.get(self.key(ticket, dry_run=dry_run, approve=approve))
        if hit is None:
            return None
        cloned = copy.deepcopy(hit)
        cloned.cached = True
        return cloned

    def save(self, ticket: Ticket, result: RunResult, *, dry_run: bool, approve: bool) -> None:
        stored = copy.deepcopy(result)
        stored.cached = False
        self._data[self.key(ticket, dry_run=dry_run, approve=approve)] = stored

    def __len__(self) -> int:
        return len(self._data)
