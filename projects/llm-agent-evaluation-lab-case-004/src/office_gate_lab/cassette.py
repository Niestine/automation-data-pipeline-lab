"""Recorded planner and finish completions, keyed by kind and request hash.

The hash covers the trusted view only. Tool bodies are not part of the key,
so a document cannot select a different plan.
"""

from __future__ import annotations

from typing import Any

from .codec import digest
from .errors import CassetteMiss
from .tools import TOOL_NAMES


def planner_view(task: dict[str, Any]) -> dict[str, Any]:
    return {"request": task["request"], "tools": list(TOOL_NAMES)}


def finish_view(task: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "finish", "task_id": task["id"]}


class Cassette:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = {task["id"]: task for task in tasks}
        self.index: dict[tuple[str, str], dict[str, Any]] = {}
        for task in tasks:
            self.index[("plan", digest(planner_view(task)))] = task["plan"]
            self.index[("finish", digest(finish_view(task)))] = task["finish"]

    def complete(self, kind: str, view: dict[str, Any]) -> dict[str, Any]:
        key = (kind, digest(view))
        if key not in self.index:
            raise CassetteMiss(f"no {kind} completion for the trusted view")
        return self.index[key]
