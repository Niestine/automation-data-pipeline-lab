"""Path bootstrap and fixture loaders for the office-gate tests."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from office_gate_lab.runner import OfficeRunner  # noqa: E402


def load_world() -> dict:
    return json.loads((EXAMPLES / "world.json").read_text(encoding="utf-8"))


def load_tasks() -> list[dict]:
    payload = json.loads((EXAMPLES / "tasks.json").read_text(encoding="utf-8"))
    return payload["tasks"]


def task_by_id(tasks: list[dict], task_id: str) -> dict:
    return next(task for task in tasks if task["id"] == task_id)


def clone_task(task: dict, task_id: str, request: str) -> dict:
    copied = copy.deepcopy(task)
    copied["id"] = task_id
    copied["request"] = request
    return copied


def runner_for(world: dict, tasks: list[dict], **kwargs) -> OfficeRunner:
    return OfficeRunner(world, tasks, **kwargs)
