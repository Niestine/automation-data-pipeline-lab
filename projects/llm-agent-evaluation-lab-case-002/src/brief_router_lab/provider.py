"""Provider-neutral planner interface plus local stand-ins.

Nothing in this package calls a hosted LLM. FakePlanner is scripted and
deterministic. HeuristicPlanner is a keyword router used so the CLI stays offline.
"""

from __future__ import annotations

from typing import Any, Protocol
import json
import re

from .models import CompletionRequest, CompletionResponse, SCHEMA_NAME
from .registry import planned_cost


class ProviderError(Exception):
    def __init__(self, message: str, *, code: str, transient: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.transient = transient
        self.message = message


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        ...


class FakePlanner:
    """Replay scripted plans keyed by packet id.

    Each packet maps to a list of steps. A step is a plan object, a text
    payload, or a raised ProviderError. Extra attempts reuse the last step.
    """

    def __init__(self, scripts: dict[str, list[dict[str, Any]]], *, model: str = "fake-router-v1") -> None:
        self.name = "fake"
        self.model = model
        self.scripts = scripts
        self.call_log: list[CompletionRequest] = []
        self._cursors: dict[str, int] = {}

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.call_log.append(request)
        steps = self.scripts.get(request.task_id)
        if not steps:
            raise ProviderError(
                f"no script for task {request.task_id}",
                code="no_script",
                transient=False,
            )
        index = self._cursors.get(request.task_id, 0)
        step = steps[min(index, len(steps) - 1)]
        self._cursors[request.task_id] = index + 1
        if step.get("error"):
            raise ProviderError(
                str(step.get("message") or "provider error"),
                code=str(step.get("code") or "provider_error"),
                transient=bool(step.get("transient", False)),
            )
        if "plan" in step:
            text = json.dumps(step["plan"], sort_keys=True)
        elif "text" in step:
            text = str(step["text"])
        else:
            raise ProviderError(
                f"script step for task {request.task_id} has neither plan, text, nor error",
                code="bad_script",
                transient=False,
            )
        return CompletionResponse(
            text=text,
            model=self.model,
            latency_ms=int(step.get("latency_ms", 5)),
        )

    def reset(self) -> None:
        self.call_log.clear()
        self._cursors.clear()


_ASSET = re.compile(r"AST-\d+", re.IGNORECASE)
_NOTE = re.compile(r"NOTE-\d+", re.IGNORECASE)
_SLOT = re.compile(r"SLOT-[A-Z0-9]+", re.IGNORECASE)
_LIMIT = re.compile(r"limit\s+(\d+)", re.IGNORECASE)
_FOR_CLIPS = re.compile(r"for\s+(.+?)\s+clips", re.IGNORECASE)


def _plan(
    goal_kind: str,
    steps: list[dict[str, Any]],
    *,
    confidence: float = 0.91,
    needs_human: bool = False,
) -> dict[str, Any]:
    names = [str(item["tool"]) for item in steps]
    return {
        "schema": SCHEMA_NAME,
        "goal_kind": goal_kind,
        "confidence": confidence,
        "budget_tokens": max(1, planned_cost(names)),
        "needs_human": needs_human,
        "steps": steps,
        "rationale": f"heuristic:{goal_kind}",
    }


def _step(step_id: str, tool: str, args: dict[str, Any] | None = None, bind: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "id": step_id,
        "tool": tool,
        "args": dict(args or {}),
        "bind": dict(bind or {}),
    }


class HeuristicPlanner:
    """Deterministic keyword stand-in. Not a language model."""

    name = "heuristic"
    model = "heuristic-router-v1"

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            payload = json.loads(request.user)
        except json.JSONDecodeError as exc:
            raise ProviderError("user payload is not JSON", code="bad_request", transient=False) from exc
        goal = str(payload.get("goal", ""))
        lowered = goal.lower()
        assets = [match.upper() for match in _ASSET.findall(goal)]
        notes = [match.upper() for match in _NOTE.findall(goal)]
        slots = [match.upper() for match in _SLOT.findall(goal)]
        asset_id = assets[0] if assets else None
        note_id = notes[0] if notes else None
        slot_id = slots[0] if slots else None

        if any(marker in lowered for marker in ("refuse", "out of scope", "cannot help")):
            body = _plan("refuse", [_step("s1", "refuse", {"reason": "out of scope"})])
        elif any(marker in lowered for marker in ("hold", "calendar", "schedule", "slot")):
            body = self._schedule(asset_id, slot_id)
        elif any(marker in lowered for marker in ("publish", "queue a public", "queue a publish")):
            body = self._publish(asset_id)
        elif "draft" in lowered or "brief" in lowered or ("recap" in lowered and "search" not in lowered):
            body = self._brief(asset_id, note_id, lowered)
        elif "search" in lowered:
            body = self._search(goal, lowered)
        elif any(marker in lowered for marker in ("look up", "lookup", "get restricted", "get catalog", "asset")):
            body = self._get(asset_id)
        else:
            body = _plan("refuse", [_step("s1", "refuse", {"reason": "unrecognized goal"})])

        return CompletionResponse(
            text=json.dumps(body, sort_keys=True),
            model=self.model,
            latency_ms=1,
        )

    def _search(self, goal: str, lowered: str) -> dict[str, Any]:
        limit_match = _LIMIT.search(goal)
        limit = int(limit_match.group(1)) if limit_match else 3
        limit = max(1, min(10, limit))
        clips = _FOR_CLIPS.search(goal)
        if clips:
            query = clips.group(1).strip()
        elif "weekly recap" in lowered:
            query = "weekly recap"
        else:
            query = "catalog"
        return _plan("lookup", [_step("s1", "catalog.search", {"query": query, "limit": limit})])

    def _get(self, asset_id: str | None) -> dict[str, Any]:
        return _plan(
            "lookup",
            [_step("s1", "catalog.get", {"asset_id": asset_id or "AST-000"})],
        )

    def _brief(self, asset_id: str | None, note_id: str | None, lowered: str) -> dict[str, Any]:
        asset = asset_id or "AST-101"
        steps = [
            _step("s1", "catalog.get", {"asset_id": asset}),
        ]
        next_id = 2
        compose_id = f"s{next_id}"
        if note_id:
            steps.append(_step(f"s{next_id}", "kb.get", {"note_id": note_id}))
            next_id += 1
            compose_id = f"s{next_id}"
        steps.append(
            _step(
                compose_id,
                "draft.compose",
                {
                    "title": "Recap brief",
                    "source_ids": [asset],
                    "notes": f"from {asset}",
                },
            )
        )
        next_id += 1
        wants_cite = note_id is not None or "cite" in lowered
        if wants_cite and note_id:
            steps.append(
                _step(
                    f"s{next_id}",
                    "draft.cite",
                    {"note_id": note_id},
                    {"draft_id": f"${compose_id}.draft_id"},
                )
            )
            next_id += 1
        if "submit" in lowered or "review" in lowered:
            steps.append(
                _step(
                    f"s{next_id}",
                    "review.submit",
                    {},
                    {"draft_id": f"${compose_id}.draft_id"},
                )
            )
        return _plan("research_brief", steps)

    def _publish(self, asset_id: str | None) -> dict[str, Any]:
        asset = asset_id or "AST-101"
        return _plan(
            "publish",
            [
                _step("s1", "catalog.get", {"asset_id": asset}),
                _step(
                    "s2",
                    "draft.compose",
                    {"title": "Recap brief", "source_ids": [asset], "notes": f"from {asset}"},
                ),
                _step("s3", "review.submit", {}, {"draft_id": "$s2.draft_id"}),
                _step("s4", "publish.queue", {}, {"draft_id": "$s2.draft_id"}),
            ],
        )

    def _schedule(self, asset_id: str | None, slot_id: str | None) -> dict[str, Any]:
        asset = asset_id or "AST-101"
        slot = slot_id or "SLOT-A"
        return _plan(
            "schedule",
            [
                _step("s1", "catalog.get", {"asset_id": asset}),
                _step(
                    "s2",
                    "draft.compose",
                    {"title": "Recap brief", "source_ids": [asset], "notes": f"from {asset}"},
                ),
                _step("s3", "calendar.hold", {"slot_id": slot}, {"draft_id": "$s2.draft_id"}),
            ],
        )
