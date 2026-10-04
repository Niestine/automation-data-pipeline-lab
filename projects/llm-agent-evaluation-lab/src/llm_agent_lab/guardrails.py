"""Input/output safety checks and role-based approval boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import json

from .models import ACTIONS, Approval, AgentOutput, Ticket


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    effect: str = "block"


INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
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

BULK_PII_MARKERS = (
    "dump all emails",
    "dump all email",
    "export all customers",
    "entire customer list",
    "all pii",
    "all emails",
)

EXTERNAL_MARKERS = (
    "send to external",
    "to external public",
    "forward to gmail",
    "upload to public",
)


def _blob(ticket: Ticket) -> str:
    return f"{ticket.subject}\n{ticket.body}".lower()


def inspect_input(ticket: Ticket) -> list[Violation]:
    violations: list[Violation] = []
    text = _blob(ticket)
    if not ticket.subject.strip() and not ticket.body.strip():
        violations.append(Violation("empty_ticket", "ticket has no subject or body"))
    if any(marker in text for marker in INJECTION_MARKERS):
        violations.append(Violation("prompt_injection", "prompt-injection marker in ticket text"))
    if any(marker in text for marker in CREDENTIAL_MARKERS):
        violations.append(Violation("credential_request", "ticket asks for credentials or secrets"))
    if any(marker in text for marker in BULK_PII_MARKERS):
        violations.append(Violation("bulk_pii_export", "ticket asks for a bulk personal-data dump"))
    if any(marker in text for marker in EXTERNAL_MARKERS):
        violations.append(Violation("external_exfiltration", "ticket asks to send data outside the lab"))
    return violations


def inspect_output(output: AgentOutput, ticket: Ticket) -> list[Violation]:
    del ticket
    violations: list[Violation] = []
    serialized = json.dumps(output.to_dict(), sort_keys=True).lower()
    if any(marker in serialized for marker in ("api_key", "password=", "secret_key", "bearer ")):
        violations.append(Violation("output_credential_leak", "model output contains credential-like text"))
    if output.proposed_action not in ACTIONS:
        violations.append(Violation("unknown_action", f"action {output.proposed_action!r} is not registered"))
    return violations


MIN_AUTO_CONFIDENCE = 0.6

# Actions that hand control to a human or decline anyway; never held.
SAFE_TERMINAL_ACTIONS = frozenset({"escalate", "refuse"})
READ_ONLY_ACTIONS = frozenset({"lookup_record", "summarize"})


def approval_for(output: AgentOutput, ticket: Ticket) -> str:
    action = output.proposed_action
    role = ticket.requester_role
    if action in SAFE_TERMINAL_ACTIONS:
        return Approval.AUTO_ALLOW
    if action in READ_ONLY_ACTIONS:
        # The model's own uncertainty signal can only tighten the policy.
        if output.needs_human or output.confidence < MIN_AUTO_CONFIDENCE:
            return Approval.REQUIRE_APPROVAL
        return Approval.AUTO_ALLOW
    if action == "update_record":
        if role == "intern":
            return Approval.DENY
        return Approval.REQUIRE_APPROVAL
    if action == "export_data":
        if role == "admin":
            return Approval.REQUIRE_APPROVAL
        return Approval.DENY
    return Approval.DENY
