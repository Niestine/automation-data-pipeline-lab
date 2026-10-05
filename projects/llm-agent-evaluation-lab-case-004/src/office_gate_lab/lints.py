"""Deterministic trace lints for five MAST modes that need no judge.

Inter-agent modes are left unlabeled. Ledger replays and idempotent retries
are not step repetition.
"""

from __future__ import annotations

from typing import Any


def labels_for(row: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    executed = [
        call["tool"]
        for call in row["calls"]
        if not call.get("ledger_replay") and not call.get("idempotent_retry")
    ]
    if executed != row["contract_tools"]:
        labels.append("disobey_task_spec")
    seen: list[tuple[str, str]] = []
    for call in row["calls"]:
        if call.get("ledger_replay") or call.get("idempotent_retry"):
            continue
        key = (call["tool"], str(call.get("arguments_hash")))
        if key in seen:
            labels.append("step_repetition")
            break
        seen.append(key)
    actual = row["outbox"]
    golden = row["golden_outbox"]
    if row["status"] in {"finished", "cancelled", "declined", "refused", "error"} and actual != golden:
        labels.append("premature_termination")
    verification = row.get("verification")
    if verification is None:
        labels.append("no_verification")
    elif verification.get("passed") is True and actual != golden:
        labels.append("incorrect_verification")
    return labels


def row_from_run(
    spans: list[dict[str, Any]],
    *,
    contract_tools: list[str],
    golden_outbox: list[dict[str, str]],
    outbox: list[dict[str, str]],
    status: str,
) -> dict[str, Any]:
    calls = []
    verification = None
    for span in spans:
        attributes = span["attributes"]
        if attributes["gen_ai.operation.name"] == "execute_tool":
            calls.append(
                {
                    "tool": attributes["gen_ai.tool.name"],
                    "arguments_hash": attributes.get("lab.tool.arguments_hash"),
                    "ledger_replay": bool(attributes.get("lab.ledger.replay")),
                    "idempotent_retry": bool(attributes.get("lab.idempotent_retry")),
                }
            )
        if attributes.get("lab.span.role") == "verification":
            verification = {"passed": attributes.get("lab.verification.passed")}
    return {
        "calls": calls,
        "contract_tools": contract_tools,
        "golden_outbox": [_pair(item) for item in golden_outbox],
        "outbox": [_pair(item) for item in outbox],
        "status": status,
        "verification": verification,
    }


def _pair(item: dict[str, str]) -> dict[str, str]:
    return {"document_id": item["document_id"], "recipient": item["recipient"]}


def lint_run(
    spans: list[dict[str, Any]],
    *,
    contract_tools: list[str],
    golden_outbox: list[dict[str, str]],
    outbox: list[dict[str, str]],
    status: str,
) -> list[str]:
    return labels_for(
        row_from_run(
            spans,
            contract_tools=contract_tools,
            golden_outbox=golden_outbox,
            outbox=outbox,
            status=status,
        )
    )


def approval_coverage(spans: list[dict[str, Any]]) -> bool:
    """Every mutating execute_tool has a prior accept or a pre-approved policy
    span for the same gen_ai.tool.call.id. An approval for one call does not
    cover a different send."""
    for index, span in enumerate(spans):
        attributes = span["attributes"]
        if attributes["gen_ai.operation.name"] != "execute_tool":
            continue
        if not attributes.get("lab.tool.mutates"):
            continue
        if attributes.get("lab.ledger.replay") or attributes.get("lab.idempotent_retry"):
            continue
        call_id = attributes.get("gen_ai.tool.call.id")
        covered = False
        for prior in spans[:index]:
            prior_attributes = prior["attributes"]
            if prior_attributes.get("gen_ai.tool.call.id") != call_id:
                continue
            operation = prior_attributes["gen_ai.operation.name"]
            if operation == "request_approval" and prior_attributes.get("lab.approval.action") == "accept":
                covered = True
            if (
                operation == "policy_check"
                and prior_attributes.get("lab.policy.decision") == "allow"
                and prior_attributes.get("lab.policy.preapproved") is True
            ):
                covered = True
        if not covered:
            return False
    return True
