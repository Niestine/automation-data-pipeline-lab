"""Scripted provider client. Inner SDK retries stay off."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


SLIP_OK = '{"berth":"B-1","vessel":"MV EXAMPLE","window":"06:00-08:00"}'


@dataclass
class ScriptStep:
    status: int = 200
    text: str = SLIP_OK
    error_type: str | None = None
    error_code: str | None = None
    error_message: str = ""
    headers: dict = field(default_factory=dict)
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    request_id: str = ""
    sse_error: bool = False
    timeout: bool = False
    network: bool = False
    spend_limit: bool = False
    usage_present: bool = True


@dataclass
class AttemptResult:
    model_id: str
    status: int
    text: str
    error_type: str | None
    error_code: str | None
    error_message: str
    headers: dict
    input_tokens: int | None
    output_tokens: int | None
    request_id: str
    sse_error: bool
    timeout: bool
    network: bool
    spend_limit: bool
    timeout_s: float

    @property
    def ok(self) -> bool:
        return (
            self.status == 200
            and not self.sse_error
            and not self.timeout
            and not self.network
            and not self.error_code
            and not self.error_type
        )


class FakeProvider:
    """One HTTP attempt per `complete` call. `max_retries` is fixed at 0."""

    def __init__(self, scripts: dict[str, list[ScriptStep]] | None = None) -> None:
        self.max_retries = 0
        self.scripts: dict[str, list[ScriptStep]] = {
            model_id: list(steps) for model_id, steps in (scripts or {}).items()
        }
        self._index: dict[str, int] = {}
        self.calls: list[dict] = []

    def complete(self, model_id: str, query_text: str, timeout_s: float) -> AttemptResult:
        if self.max_retries != 0:
            raise RuntimeError("provider SDK retries must stay at 0")
        steps = self.scripts.get(model_id) or [ScriptStep()]
        index = self._index.get(model_id, 0)
        step = steps[min(index, len(steps) - 1)]
        if index < len(steps):
            self._index[model_id] = index + 1
        self.calls.append(
            {
                "model_id": model_id,
                "query": query_text,
                "timeout_s": timeout_s,
                "status": step.status,
                "error_code": step.error_code,
            }
        )
        input_tokens = step.input_tokens
        output_tokens = step.output_tokens
        if not step.usage_present:
            input_tokens = None
            output_tokens = None
        return AttemptResult(
            model_id=model_id,
            status=step.status,
            text=step.text,
            error_type=step.error_type,
            error_code=step.error_code,
            error_message=step.error_message,
            headers=dict(step.headers),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            request_id=step.request_id,
            sse_error=step.sse_error,
            timeout=step.timeout,
            network=step.network,
            spend_limit=step.spend_limit,
            timeout_s=timeout_s,
        )


def parse_slip(text: str) -> dict | None:
    """Closed berth slip: berth, vessel, and window, each a non-empty string."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if set(obj) != {"berth", "vessel", "window"}:
        return None
    for key in ("berth", "vessel", "window"):
        if not isinstance(obj[key], str) or not obj[key].strip():
            return None
    return obj
