"""Plan-level admission, classification checks, and approval tightening."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional
import json

from .bindings import BindError, step_id_from_expr
from .contracts import validate_schema
from .models import (
    CLASS_RANK,
    ROLE_RANK,
    Approval,
    ToolPlan,
    WorkPacket,
)
from .registry import WRITE_SIDE_EFFECTS, allowed_tools, get_tool, planned_cost


LookupFn = Callable[[str, str], Optional[str]]


INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
    "reveal the system prompt",
    "reveal your instructions",
    "system prompt",
    "jailbreak",
    "<system>",
)

CREDENTIAL_MARKERS = (
    "api_key",
    "api key",
    "password=",
    "secret_key",
    "bearer ",
)

EXTERNAL_MARKERS = (
    "send this brief to an external",
    "to an external public",
    "to external public",
    "forward to gmail",
    "upload to public bucket",
)

MIN_AUTO_CONFIDENCE = 0.55


@dataclass(frozen=True)
class Violation:
    code: str
    message: str


@dataclass(frozen=True)
class Admission:
    ok: bool
    code: Optional[str] = None
    message: str = ""
    tokens_planned: int = 0


def inspect_input(packet: WorkPacket) -> list[Violation]:
    violations: list[Violation] = []
    goal = packet.goal.lower()
    if not packet.goal.strip():
        violations.append(Violation("empty_goal", "packet has an empty goal"))
    if any(marker in goal for marker in INJECTION_MARKERS):
        violations.append(Violation("prompt_injection", "prompt-injection marker in goal text"))
    if any(marker in goal for marker in CREDENTIAL_MARKERS):
        violations.append(Violation("credential_request", "goal asks for credentials or secrets"))
    if any(marker in goal for marker in EXTERNAL_MARKERS):
        violations.append(Violation("external_exfiltration", "goal asks to send data outside the lab"))
    return violations


def inspect_plan_text(plan: ToolPlan) -> list[Violation]:
    """Scan planner-authored text (rationale and step args) for credential-like strings."""
    args_blob = json.dumps([step.args for step in plan.steps], sort_keys=True, default=str)
    blob = f"{plan.rationale} {args_blob}".lower()
    if any(marker in blob for marker in CREDENTIAL_MARKERS):
        return [Violation("output_credential_leak", "plan contains credential-like text")]
    return []


def _readable(workspace: str, classification: str) -> bool:
    return CLASS_RANK[classification] <= CLASS_RANK[workspace]


def _static_classification(
    spec_lookup_kind: str | None,
    spec_lookup_arg: str | None,
    args: dict[str, Any],
    lookup: LookupFn,
    workspace: str,
) -> Optional[Violation]:
    if not spec_lookup_kind or not spec_lookup_arg:
        return None
    if spec_lookup_arg not in args:
        return None
    item_id = args[spec_lookup_arg]
    if not isinstance(item_id, str):
        return None
    classification = lookup(spec_lookup_kind, item_id)
    if classification is None:
        return None
    if not _readable(workspace, classification):
        return Violation(
            "classification_denied",
            f"{spec_lookup_kind} {item_id} is {classification} in {workspace} workspace",
        )
    return None


def _source_classification(args: dict[str, Any], lookup: LookupFn, workspace: str) -> Optional[Violation]:
    source_ids = args.get("source_ids")
    if not isinstance(source_ids, list):
        return None
    for item_id in source_ids:
        if not isinstance(item_id, str):
            continue
        classification = lookup("asset", item_id)
        if classification is None:
            # A draft must not cite a source the catalog cannot vouch for.
            return Violation("unknown_source", f"source {item_id} is not in the catalog")
        if not _readable(workspace, classification):
            return Violation(
                "classification_denied",
                f"source {item_id} is {classification} in {workspace} workspace",
            )
    return None


def _forbidden_sequence(plan: ToolPlan) -> Optional[Violation]:
    seen: list[str] = []
    for step in plan.steps:
        if step.tool == "publish.queue" and "review.submit" not in seen:
            return Violation(
                "forbidden_sequence",
                "publish.queue requires an earlier review.submit in the same plan",
            )
        if step.tool == "draft.cite" and "draft.compose" not in seen:
            return Violation(
                "forbidden_sequence",
                "draft.cite requires an earlier draft.compose in the same plan",
            )
        if step.tool == "calendar.hold" and "draft.compose" not in seen:
            return Violation(
                "forbidden_sequence",
                "calendar.hold requires an earlier draft.compose in the same plan",
            )
        seen.append(step.tool)
    return None


def admit_plan(plan: ToolPlan, packet: WorkPacket, lookup: LookupFn) -> Admission:
    ids = [step.id for step in plan.steps]
    if len(ids) != len(set(ids)):
        return Admission(False, "duplicate_step", "step ids must be unique")

    if len(plan.steps) > packet.max_steps:
        return Admission(False, "step_limit", f"plan has {len(plan.steps)} steps; max is {packet.max_steps}")

    allow = allowed_tools(plan.goal_kind)
    prior: set[str] = set()
    compose_steps: set[str] = set()
    role_rank = ROLE_RANK[packet.operator_role]

    for step in plan.steps:
        spec = get_tool(step.tool)
        if spec is None:
            return Admission(False, "unknown_tool", f"tool {step.tool!r} is not registered")
        if step.tool not in allow:
            return Admission(
                False,
                "capability_mismatch",
                f"tool {step.tool} is not in the {plan.goal_kind} allowlist",
            )
        if role_rank < ROLE_RANK[spec.min_role]:
            return Admission(
                False,
                "role_denied",
                f"role {packet.operator_role} cannot call {step.tool}",
            )
        if packet.workspace not in spec.allowed_workspaces:
            return Admission(
                False,
                "workspace_denied",
                f"workspace {packet.workspace} cannot call {step.tool}",
            )
        for key, expr in step.bind.items():
            try:
                source_id = step_id_from_expr(expr)
            except BindError as exc:
                return Admission(False, exc.code, exc.message)
            if source_id not in prior:
                return Admission(
                    False,
                    "bind_forward_ref",
                    f"step {step.id} binds {key} to future or unknown step {source_id}",
                )
        overlap = sorted(set(step.args) & set(step.bind))
        if overlap:
            return Admission(
                False,
                "tool_schema",
                f"step {step.id} sets {overlap} in both args and bind",
            )
        if "draft_id" in spec.arg_schema.get("properties", {}):
            # Draft tools may only touch a draft composed earlier in this same plan.
            # A static draft_id could reach another packet's (or workspace's) draft.
            expr = step.bind.get("draft_id")
            if expr is None or step_id_from_expr(expr) not in compose_steps:
                return Admission(
                    False,
                    "draft_provenance",
                    f"step {step.id} must bind draft_id from an earlier draft.compose step",
                )
        required = spec.arg_schema.get("required", [])
        present = set(step.args) | set(step.bind)
        missing = [key for key in required if key not in present]
        if missing:
            return Admission(
                False,
                "tool_schema",
                f"step {step.id} missing required args {missing}",
            )
        # Static args (not filled by bind) can be schema-checked now for the keys present.
        static_schema = {
            "type": "object",
            "additionalProperties": spec.arg_schema.get("additionalProperties", False),
            "properties": spec.arg_schema.get("properties", {}),
        }
        static_errors = validate_schema(static_schema, step.args, f"$.{step.id}.args")
        if static_errors:
            return Admission(False, "tool_schema", "; ".join(static_errors))
        class_hit = _static_classification(
            spec.lookup_kind,
            spec.lookup_arg,
            step.args,
            lookup,
            packet.workspace,
        )
        if class_hit:
            return Admission(False, class_hit.code, class_hit.message)
        if step.tool == "draft.compose":
            source_hit = _source_classification(step.args, lookup, packet.workspace)
            if source_hit:
                return Admission(False, source_hit.code, source_hit.message)
        prior.add(step.id)
        if step.tool == "draft.compose":
            compose_steps.add(step.id)

    sequence = _forbidden_sequence(plan)
    if sequence:
        return Admission(False, sequence.code, sequence.message)

    tokens = planned_cost(plan.tool_names())
    if tokens > packet.token_budget or plan.budget_tokens > packet.token_budget:
        return Admission(
            False,
            "budget_exceeded",
            f"planned {tokens} tokens / declared {plan.budget_tokens} exceeds budget {packet.token_budget}",
            tokens_planned=tokens,
        )
    return Admission(True, tokens_planned=tokens)


def approval_for(plan: ToolPlan) -> str:
    write_tools = [
        step.tool
        for step in plan.steps
        if (spec := get_tool(step.tool)) is not None and spec.side_effect in WRITE_SIDE_EFFECTS
    ]
    if write_tools and (plan.needs_human or plan.confidence < MIN_AUTO_CONFIDENCE):
        return Approval.REQUIRE_APPROVAL
    return Approval.AUTO_ALLOW


def runtime_classification(
    tool: str,
    args: dict[str, Any],
    packet: WorkPacket,
    lookup: LookupFn,
) -> Optional[Violation]:
    spec = get_tool(tool)
    if spec is None:
        return Violation("unknown_tool", f"tool {tool!r} is not registered")
    hit = _static_classification(spec.lookup_kind, spec.lookup_arg, args, lookup, packet.workspace)
    if hit:
        return hit
    if tool == "draft.compose":
        return _source_classification(args, lookup, packet.workspace)
    return None
