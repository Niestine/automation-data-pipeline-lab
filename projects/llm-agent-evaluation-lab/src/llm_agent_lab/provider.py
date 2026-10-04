"""Provider-neutral completion interface plus local stand-ins.

Nothing in this package calls a hosted LLM. FakeProvider is scripted and
deterministic. HeuristicProvider is a keyword classifier used so the CLI
can run offline without a response script.
"""

from __future__ import annotations

from typing import Any, Protocol
import json
import re

from .models import CompletionRequest, CompletionResponse


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


class FakeProvider:
    """Replay scripted completions keyed by task/ticket id.

    Each ticket maps to a list of steps. A step is either a text payload
    or a raised ProviderError. Extra attempts reuse the last step.
    """

    def __init__(self, scripts: dict[str, list[dict[str, Any]]], *, model: str = "fake-lab-v1") -> None:
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
        if "text" not in step:
            raise ProviderError(
                f"script step for task {request.task_id} has neither text nor error",
                code="bad_script",
                transient=False,
            )
        return CompletionResponse(
            text=str(step["text"]),
            model=self.model,
            latency_ms=int(step.get("latency_ms", 5)),
        )

    def reset(self) -> None:
        self.call_log.clear()
        self._cursors.clear()


_RECORD_ID = re.compile(r"ORD-\d+", re.IGNORECASE)


class HeuristicProvider:
    """Deterministic keyword stand-in. Not a language model."""

    name = "heuristic"
    model = "heuristic-rules-v1"

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        try:
            payload = json.loads(request.user)
        except json.JSONDecodeError as exc:
            raise ProviderError("user payload is not JSON", code="bad_request", transient=False) from exc
        text = f"{payload.get('subject', '')} {payload.get('body', '')}"
        lowered = text.lower()
        record_ids = [match.upper() for match in _RECORD_ID.findall(text)]
        record_id = record_ids[0] if record_ids else None

        if any(marker in lowered for marker in ("export", "dump all", "entire customer", "all emails")):
            intent, action = "data_export", "export_data"
        elif any(marker in lowered for marker in ("update", "change status", "set status")):
            intent, action = "record_update", "update_record"
        elif any(marker in lowered for marker in ("escalate", "manager", "pager")):
            intent, action = "escalate", "escalate"
        elif any(marker in lowered for marker in ("summarize", "summary", "tldr")):
            intent, action = "summarize", "summarize"
        elif any(marker in lowered for marker in ("status", "where is", "lookup", "look up", "track")):
            intent, action = "status_lookup", "lookup_record"
        else:
            intent, action = "unknown", "refuse"

        args: dict[str, Any] = {}
        entities: dict[str, Any] = {}
        if record_id:
            args["record_id"] = record_id
            entities["record_id"] = record_id
        if action == "update_record":
            status_match = re.search(r"status\s+to\s+([a-z]+)", lowered)
            args["fields"] = {"status": status_match.group(1) if status_match else "pending"}
        if action == "export_data":
            if "aggregate" in lowered or "count" in lowered:
                args["scope"] = "aggregate_counts"
            else:
                args["scope"] = "full"
        if action == "summarize":
            args["summary"] = str(payload.get("subject") or "internal ticket").strip()
        if action == "escalate":
            args["queue"] = "ops-leads"

        body = {
            "intent": intent,
            "confidence": 0.92 if intent != "unknown" else 0.34,
            "entities": entities,
            "proposed_action": action,
            "action_args": args,
            "rationale": f"heuristic:{intent}",
            "needs_human": action in {"update_record", "export_data"},
        }
        return CompletionResponse(
            text=json.dumps(body, sort_keys=True),
            model=self.model,
            latency_ms=1,
        )
