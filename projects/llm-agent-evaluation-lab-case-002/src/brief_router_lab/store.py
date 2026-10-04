"""Packet-level run cache and step-level idempotency ledger."""

from __future__ import annotations

from typing import Optional
import copy
import hashlib
import json

from .models import CONTRACT_VERSION, RunResult, StepResult, WorkPacket


class RunStore:
    def __init__(self) -> None:
        self._data: dict[str, RunResult] = {}

    def key(self, packet: WorkPacket, *, dry_run: bool, approve: bool) -> str:
        mode = "dry" if dry_run else "live"
        gate = "approved" if approve else "auto"
        return f"{packet.packet_id}:{CONTRACT_VERSION}:{packet.input_hash()}:{mode}:{gate}"

    def run_id(self, packet: WorkPacket, *, dry_run: bool, approve: bool) -> str:
        material = self.key(packet, dry_run=dry_run, approve=approve).encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:12]

    def get(self, packet: WorkPacket, *, dry_run: bool, approve: bool) -> Optional[RunResult]:
        hit = self._data.get(self.key(packet, dry_run=dry_run, approve=approve))
        if hit is None:
            return None
        cloned = copy.deepcopy(hit)
        cloned.cached = True
        return cloned

    def save(self, packet: WorkPacket, result: RunResult, *, dry_run: bool, approve: bool) -> None:
        stored = copy.deepcopy(result)
        stored.cached = False
        self._data[self.key(packet, dry_run=dry_run, approve=approve)] = stored

    def __len__(self) -> int:
        return len(self._data)


def step_key(packet: WorkPacket, step_id: str, args: dict, *, dry_run: bool) -> str:
    """Ledger key. The packet input hash covers role and workspace, so a reused
    packet id under a different workspace never replays another clearance's reads."""
    mode = "dry" if dry_run else "live"
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{packet.packet_id}:{packet.input_hash()}:{step_id}:{digest}:{mode}"


class StepStore:
    def __init__(self) -> None:
        self._data: dict[str, StepResult] = {}

    def get(self, key: str) -> Optional[StepResult]:
        hit = self._data.get(key)
        if hit is None:
            return None
        cloned = copy.deepcopy(hit)
        cloned.replayed = True
        return cloned

    def save(self, key: str, result: StepResult) -> None:
        stored = copy.deepcopy(result)
        stored.replayed = False
        self._data[key] = stored

    def __len__(self) -> int:
        return len(self._data)
