"""JSON schedule artifacts. Replay reads ``schedule`` and ignores priorities."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from schedlab.errors import ArtifactError

SCHEMA = 1

_REQUIRED = (
    "schema",
    "subject",
    "revision",
    "policy",
    "seed",
    "n_max",
    "k",
    "d",
    "change_points",
    "schedule",
    "preemptions",
    "delays",
    "steps",
    "terminal",
    "schedules_used",
    "gil",
    "python",
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass
class Report:
    subject: str
    revision: str
    policy: str
    seed: int
    n_max: int
    k: int
    d: int
    change_points: list[int]
    schedule: list[int]
    preemptions: int
    delays: int
    steps: int
    terminal: str
    schedules_used: int
    gil: dict[str, Any]
    python: str
    coverage: str = ""
    final_cells: dict[str, int] = field(default_factory=dict)
    thread_locals: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_reentrancy: int = 0
    notes: list[str] = field(default_factory=list)
    pad: int = 0
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "subject": self.subject,
            "revision": self.revision,
            "policy": self.policy,
            "seed": self.seed,
            "n_max": self.n_max,
            "k": self.k,
            "d": self.d,
            "change_points": list(self.change_points),
            "schedule": list(self.schedule),
            "preemptions": self.preemptions,
            "delays": self.delays,
            "steps": self.steps,
            "terminal": self.terminal,
            "schedules_used": self.schedules_used,
            "gil": dict(self.gil),
            "python": self.python,
            "coverage": self.coverage,
            "final_cells": dict(self.final_cells),
            "thread_locals": self.thread_locals,
            "max_reentrancy": self.max_reentrancy,
            "pad": self.pad,
        }
        return payload

    def stem(self) -> str:
        return artifact_stem(
            self.subject, self.revision, self.policy, self.d, self.seed, pad=self.pad
        )


def artifact_stem(
    subject: str, revision: str, policy: str, d: int, seed: int, *, pad: int = 0
) -> str:
    for label, value in (("subject", subject), ("revision", revision), ("policy", policy)):
        if not value or any(char in value for char in "/\\"):
            raise ArtifactError(f"unsafe {label}")
    # A padded subject is a different op list; keep its files apart.
    name = f"{subject}-pad{pad}" if pad else subject
    return f"{name}-{revision}-{policy}-d{d}-seed{seed}"


def report_path(directory: Path, report: Report) -> Path:
    return Path(directory) / f"{report.stem()}.json"


def dump(path: Path, report: Report) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


def load(path: Path) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"cannot read {target.name}") from exc
    validate(payload)
    return payload


def validate(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ArtifactError("artifact must be an object")
    missing = [key for key in _REQUIRED if key not in payload]
    if missing:
        raise ArtifactError(f"missing fields: {', '.join(missing)}")
    if payload["schema"] != SCHEMA:
        raise ArtifactError("unsupported schema")
    if payload["revision"] not in ("buggy", "fixed"):
        raise ArtifactError("revision must be buggy or fixed")
    for key in ("seed", "n_max", "k", "d", "preemptions", "delays", "steps", "schedules_used"):
        if not _is_int(payload[key]):
            raise ArtifactError(f"{key} must be an int")
    if payload["n_max"] < 1 or payload["k"] < 1:
        raise ArtifactError("n_max and k must be >= 1")
    _int_list(payload["change_points"], "change_points")
    schedule = _int_list(payload["schedule"], "schedule")
    if any(tid < 1 for tid in schedule):
        raise ArtifactError("schedule tids must be >= 1")
    if not isinstance(payload["terminal"], str) or not payload["terminal"]:
        raise ArtifactError("terminal must be a string")
    if not isinstance(payload["subject"], str) or not payload["subject"]:
        raise ArtifactError("subject must be a string")
    if not isinstance(payload["policy"], str) or not payload["policy"]:
        raise ArtifactError("policy must be a string")
    gil = payload["gil"]
    if not isinstance(gil, dict):
        raise ArtifactError("gil must be an object")
    for key in ("Py_GIL_DISABLED", "PYTHON_GIL", "gil_enabled"):
        if key not in gil:
            raise ArtifactError(f"gil.{key} is required")
    if not isinstance(payload["python"], str):
        raise ArtifactError("python must be a string")
    pad = payload.get("pad", 0)
    if not _is_int(pad) or pad < 0:
        raise ArtifactError("pad must be an int >= 0")
    return payload


def _int_list(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or not all(_is_int(item) for item in value):
        raise ArtifactError(f"{label} must be a list of ints")
    return value


def schedule_of(payload: dict[str, Any]) -> list[int]:
    validate(payload)
    return list(payload["schedule"])
