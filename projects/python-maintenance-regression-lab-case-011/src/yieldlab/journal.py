"""Append-only schedule journals and defensive loaders."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def hash_events(events: list) -> str:
    payload = json.dumps(events, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_journal(
    *,
    scenario: str,
    variant: str,
    mode: str,
    seed: int,
    depth: int,
    preemption_bound: int,
    switch_interval: int,
    k_budget: int,
    n_max: int,
    priorities: dict,
    change_points: list,
    events: list,
    failure: str | None,
    invariant_id: str,
) -> dict:
    return {
        "scenario": scenario,
        "variant": variant,
        "mode": mode,
        "seed": seed,
        "depth": depth,
        "preemption_bound": preemption_bound,
        "switch_interval": switch_interval,
        "k_budget": k_budget,
        "n_max": n_max,
        "priorities": {str(k): int(v) for k, v in sorted(priorities.items(), key=lambda kv: int(kv[0]))},
        "change_points": [
            {"step": int(step), "priority": int(priority)} for step, priority in change_points
        ],
        "events": events,
        "failure": failure,
        "invariant_id": invariant_id,
        "trace_sha256": hash_events(events),
    }


def dump_journal(path: Path, journal: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_journal(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"journal is not json: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("journal must be an object")
    for key in ("scenario", "variant", "mode", "events", "trace_sha256"):
        if key not in data:
            raise ValueError(f"journal missing {key}")
    if data["mode"] not in ("gil", "free"):
        raise ValueError("mode must be gil or free")
    if not isinstance(data["scenario"], str) or not data["scenario"]:
        raise ValueError("scenario must be a non-empty string")
    if not isinstance(data["variant"], str) or not data["variant"]:
        raise ValueError("variant must be a non-empty string")
    events = data["events"]
    if not isinstance(events, list) or not events:
        raise ValueError("events must be a non-empty list")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"event {index} must be an object")
        if not isinstance(event.get("thread"), int):
            raise ValueError(f"event {index} thread must be an int")
        op = event.get("op")
        if not isinstance(op, str) or not op:
            raise ValueError(f"event {index} op must be a non-empty string")
        if "args" in event and not isinstance(event["args"], list):
            raise ValueError(f"event {index} args must be a list")
        if "i" in event and event["i"] != index:
            raise ValueError(f"event {index} i does not match position")
    digest = hash_events(events)
    if data["trace_sha256"] != digest:
        raise ValueError("trace_sha256 does not match events")
    points = data.get("change_points", [])
    if not isinstance(points, list):
        raise ValueError("change_points must be a list")
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("change point must be an object")
        if not isinstance(point.get("step"), int) or point["step"] < 1:
            raise ValueError("change point step must be a positive int")
        if not isinstance(point.get("priority"), int):
            raise ValueError("change point priority must be an int")
    return data
