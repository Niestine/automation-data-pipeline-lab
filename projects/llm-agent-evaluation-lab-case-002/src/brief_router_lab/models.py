"""Domain models for the typed tool-routing brief workbench."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional
import hashlib
import json


CONTRACT_VERSION = "2.0.0"
SCHEMA_NAME = "tool_plan_v1"

GOAL_KINDS = (
    "lookup",
    "research_brief",
    "publish",
    "schedule",
    "refuse",
)

ROLES = ("viewer", "editor", "publisher")
WORKSPACES = ("public", "internal", "restricted")
CLASSIFICATIONS = ("public", "internal", "restricted")

ROLE_RANK = {"viewer": 0, "editor": 1, "publisher": 2}
CLASS_RANK = {"public": 0, "internal": 1, "restricted": 2}


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


@dataclass(frozen=True)
class WorkPacket:
    packet_id: str
    goal: str
    operator_role: str
    workspace: str
    max_steps: int = 8
    token_budget: int = 200

    def __post_init__(self) -> None:
        if not str(self.packet_id).strip():
            raise ValueError("packet_id is required")
        if self.operator_role not in ROLES:
            raise ValueError(f"unknown operator_role: {self.operator_role}")
        if self.workspace not in WORKSPACES:
            raise ValueError(f"unknown workspace: {self.workspace}")
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool):
            raise ValueError("max_steps must be an integer")
        if self.max_steps < 1 or self.max_steps > 16:
            raise ValueError("max_steps must be between 1 and 16")
        if not isinstance(self.token_budget, int) or isinstance(self.token_budget, bool):
            raise ValueError("token_budget must be an integer")
        if self.token_budget < 1 or self.token_budget > 10000:
            raise ValueError("token_budget must be between 1 and 10000")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "goal": self.goal,
            "operator_role": self.operator_role,
            "workspace": self.workspace,
            "max_steps": self.max_steps,
            "token_budget": self.token_budget,
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


def packet_from_dict(raw: dict[str, Any]) -> WorkPacket:
    return WorkPacket(
        packet_id=str(raw["packet_id"]),
        goal=str(raw.get("goal", "")),
        operator_role=str(raw["operator_role"]),
        workspace=str(raw["workspace"]),
        max_steps=int(raw.get("max_steps", 8)),
        token_budget=int(raw.get("token_budget", 200)),
    )


@dataclass(frozen=True)
class CompletionRequest:
    task_id: str
    system: str
    user: str
    response_schema_name: str = SCHEMA_NAME
    response_format: str = "json_object"
    temperature: float = 0.0
    max_tokens: int = 1024
    attempt: int = 1


@dataclass(frozen=True)
class CompletionResponse:
    text: str
    model: str
    latency_ms: int


@dataclass
class PlanStep:
    id: str
    tool: str
    args: dict[str, Any]
    bind: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": dict(self.args),
            "bind": dict(self.bind),
        }


@dataclass
class ToolPlan:
    schema: str
    goal_kind: str
    confidence: float
    budget_tokens: int
    needs_human: bool
    steps: list[PlanStep]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "goal_kind": self.goal_kind,
            "confidence": self.confidence,
            "budget_tokens": self.budget_tokens,
            "needs_human": self.needs_human,
            "steps": [step.to_dict() for step in self.steps],
            "rationale": self.rationale,
        }

    def tool_names(self) -> list[str]:
        return [step.tool for step in self.steps]


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
class StepResult:
    step_id: str
    tool: str
    status: str
    payload: dict[str, Any]
    attempts: int = 1
    dry_run: bool = False
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    packet_id: str
    status: str
    contract_version: str
    input_hash: str
    run_id: str
    attempts: int
    plan: Optional[ToolPlan] = None
    approval: Optional[str] = None
    step_results: list[StepResult] = field(default_factory=list)
    error_code: Optional[str] = None
    tokens_planned: int = 0
    tokens_used: int = 0
    token_budget: int = 0
    steps_planned: int = 0
    trace: list[TraceEvent] = field(default_factory=list)
    cached: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "status": self.status,
            "contract_version": self.contract_version,
            "input_hash": self.input_hash,
            "run_id": self.run_id,
            "attempts": self.attempts,
            "plan": None if self.plan is None else self.plan.to_dict(),
            "approval": self.approval,
            "step_results": [item.to_dict() for item in self.step_results],
            "error_code": self.error_code,
            "tokens_planned": self.tokens_planned,
            "tokens_used": self.tokens_used,
            "token_budget": self.token_budget,
            "steps_planned": self.steps_planned,
            "trace": [event.to_dict() for event in self.trace],
            "cached": self.cached,
            "dry_run": self.dry_run,
        }
