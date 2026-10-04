"""Domain models for the provider-neutral agent lab."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional
import hashlib
import json


CONTRACT_VERSION = "1.0.0"
SCHEMA_NAME = "agent_output_v1"

INTENTS = (
    "status_lookup",
    "summarize",
    "record_update",
    "data_export",
    "escalate",
    "unknown",
)

ACTIONS = (
    "lookup_record",
    "summarize",
    "update_record",
    "export_data",
    "escalate",
    "refuse",
)

ROLES = ("intern", "analyst", "admin")


class RunStatus:
    COMPLETED = "completed"
    PENDING_APPROVAL = "pending_approval"
    DENIED = "denied"
    BLOCKED = "blocked"
    FAILED = "failed"
    DRY_RUN = "dry_run"


class Approval:
    AUTO_ALLOW = "auto_allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    subject: str
    body: str
    requester_role: str
    channel: str = "internal_ops"

    def __post_init__(self) -> None:
        if not str(self.ticket_id).strip():
            raise ValueError("ticket_id is required")
        if self.requester_role not in ROLES:
            raise ValueError(f"unknown requester_role: {self.requester_role}")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "ticket_id": self.ticket_id,
            "subject": self.subject,
            "body": self.body,
            "requester_role": self.requester_role,
            "channel": self.channel,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def input_hash(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return digest[:16]


def ticket_from_dict(raw: dict[str, Any]) -> Ticket:
    return Ticket(
        ticket_id=str(raw["ticket_id"]),
        subject=str(raw.get("subject", "")),
        body=str(raw.get("body", "")),
        requester_role=str(raw["requester_role"]),
        channel=str(raw.get("channel", "internal_ops")),
    )


@dataclass(frozen=True)
class CompletionRequest:
    task_id: str
    system: str
    user: str
    response_schema_name: str = SCHEMA_NAME
    response_format: str = "json_object"
    temperature: float = 0.0
    max_tokens: int = 512
    attempt: int = 1


@dataclass(frozen=True)
class CompletionResponse:
    text: str
    model: str
    latency_ms: int


@dataclass
class AgentOutput:
    intent: str
    confidence: float
    entities: dict[str, Any]
    proposed_action: str
    action_args: dict[str, Any]
    rationale: str
    needs_human: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceEvent:
    at_ms: int
    event: str
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"at_ms": self.at_ms, "event": self.event}
        payload.update(self.fields)
        return payload


@dataclass
class ToolResult:
    tool: str
    status: str
    payload: dict[str, Any]
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    ticket_id: str
    status: str
    contract_version: str
    input_hash: str
    run_id: str
    attempts: int
    output: Optional[AgentOutput] = None
    approval: Optional[str] = None
    tool_result: Optional[ToolResult] = None
    error_code: Optional[str] = None
    trace: list[TraceEvent] = field(default_factory=list)
    cached: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "status": self.status,
            "contract_version": self.contract_version,
            "input_hash": self.input_hash,
            "run_id": self.run_id,
            "attempts": self.attempts,
            "output": None if self.output is None else self.output.to_dict(),
            "approval": self.approval,
            "tool_result": None if self.tool_result is None else self.tool_result.to_dict(),
            "error_code": self.error_code,
            "trace": [event.to_dict() for event in self.trace],
            "cached": self.cached,
            "dry_run": self.dry_run,
        }
