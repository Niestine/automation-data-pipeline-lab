"""Append-only span tree using Development GenAI operation names.

request_approval and policy_check are lab extensions. gen_ai.conversation.id
is the caller-supplied thread id. Span ids are a counter on that thread so a
replay of the same cassette reproduces them. Bodies are redacted before a
span is stored. Raw-body hashes stay on the tracer, outside the span list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .codec import digest
from .redact import redact

CONTRACT_PATH = Path(__file__).with_name("span_contract.json")


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


class Tracer:
    def __init__(self, thread_id: str, bodies: set[str]) -> None:
        self.thread_id = thread_id
        self.trace_id = f"trace-{thread_id}"
        self.bodies = set(bodies)
        self.spans: list[dict[str, Any]] = []
        self.body_hashes: dict[str, str] = {}
        self._seq = 0
        self.workflow_id: str | None = None
        self.agent_id: str | None = None

    def note_bodies(self, hashes: dict[str, str]) -> None:
        self.body_hashes.update(hashes)

    def _span(
        self,
        operation: str,
        name: str,
        parent: str | None,
        attributes: dict[str, Any],
    ) -> dict[str, Any]:
        self._seq += 1
        span_id = f"span-{self.thread_id}-{self._seq:04d}"
        payload = {
            "span_id": span_id,
            "parent_span_id": parent,
            "trace_id": self.trace_id,
            "name": name,
            "attributes": {
                "gen_ai.operation.name": operation,
                "gen_ai.conversation.id": self.thread_id,
                "gen_ai.workflow.name": "office_send",
                "error.type": None,
                **attributes,
            },
        }
        stored = redact(payload, self.bodies)
        if not isinstance(stored, dict):
            raise RuntimeError("redacted span was not an object")
        self.spans.append(stored)
        return stored

    def open_roots(self) -> None:
        workflow = self._span(
            "invoke_workflow",
            "invoke_workflow office_send",
            None,
            {"gen_ai.input.messages": [{"role": "user", "parts": [{"type": "text", "content": "office_send"}]}]},
        )
        self.workflow_id = workflow["span_id"]
        agent = self._span(
            "invoke_agent",
            "invoke_agent office_clerk",
            self.workflow_id,
            {"gen_ai.agent.name": "office_clerk"},
        )
        self.agent_id = agent["span_id"]

    def child(self, operation: str, name: str, **attributes: Any) -> dict[str, Any]:
        return self._span(operation, name, self.agent_id, attributes)

    def mark_failure(self, error_type: str) -> None:
        for span in self.spans:
            operation = span["attributes"]["gen_ai.operation.name"]
            if operation in {"invoke_workflow", "invoke_agent"} and span["attributes"].get("error.type") is None:
                span["attributes"]["error.type"] = error_type

    def write_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(span, sort_keys=True, ensure_ascii=True) for span in self.spans]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def arguments_hash(arguments: dict[str, Any]) -> str:
    return digest(arguments)


def validate_span(span: dict[str, Any], contract: dict[str, Any] | None = None) -> list[str]:
    contract = contract or load_contract()
    errors: list[str] = []
    attributes = span.get("attributes") or {}
    for key in contract["required_attributes"]:
        if not attributes.get(key):
            errors.append(f"missing {key}")
    operation = attributes.get("gen_ai.operation.name")
    if operation not in contract["operations"]:
        errors.append(f"operation {operation}")
    if operation == "execute_tool":
        for key in contract["execute_tool_attributes"]:
            if not attributes.get(key):
                errors.append(f"missing {key}")
    if span.get("trace_id") == attributes.get("gen_ai.conversation.id"):
        errors.append("conversation id must not be the trace id")
    return errors
