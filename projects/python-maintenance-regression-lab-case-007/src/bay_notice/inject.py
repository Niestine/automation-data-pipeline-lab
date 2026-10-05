"""Scripted gate. An empty schedule is the success path; installed steps force the error."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import make_error
from .ledger import validate_job


@dataclass(frozen=True)
class Job:
    operation_id: str
    bay: str
    body: str
    charge: str

    def validate(self) -> None:
        validate_job(self.operation_id, self.bay, self.body, self.charge)


@dataclass(frozen=True)
class Step:
    kind: str
    error_name: str = ""
    error: BaseException | None = None
    token: str | None = None
    body: str = ""
    partial: str = ""
    rtt_s: float = 0.05


class Gateway:
    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps = list(steps or [])
        self.sends = 0

    def note_send(self) -> None:
        self.sends += 1

    def receive(self, outstanding: str) -> Step:
        if not self.steps:
            return Step(kind="success", token=outstanding)
        step = self.steps.pop(0)
        if step.kind in {"success", "late"} and step.token is None:
            return replace(step, token=outstanding)
        return step


def step_from_dict(raw: dict) -> Step:
    kind = raw["kind"]
    if kind not in {"success", "late", "error", "timeout", "overloaded"}:
        raise ValueError(f"unknown step kind {kind!r}")
    error = None
    error_name = raw.get("error_name", "")
    if error_name:
        error = make_error(error_name)
    return Step(
        kind=kind,
        error_name=error_name,
        error=error,
        token=raw.get("token"),
        body=raw.get("body", ""),
        partial=raw.get("partial", ""),
        rtt_s=float(raw.get("rtt_s", 0.05)),
    )


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def job_from_dict(raw: dict) -> Job:
    job = Job(
        operation_id=raw["operation_id"],
        bay=raw["bay"],
        body=raw["body"],
        charge=raw["charge"],
    )
    job.validate()
    return job
