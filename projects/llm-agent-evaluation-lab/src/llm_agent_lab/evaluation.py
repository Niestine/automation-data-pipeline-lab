"""Gold-set scoring for agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .models import RunResult, ticket_from_dict, Ticket


CONTRACT_ERROR_CODES = frozenset({"parse_error", "schema_error"})


@dataclass(frozen=True)
class GoldCase:
    ticket_id: str
    expected_status: str
    intent: Optional[str] = None
    proposed_action: Optional[str] = None
    expected_approval: Optional[str] = None
    expected_error_code: Optional[str] = None


@dataclass
class CaseScore:
    ticket_id: str
    status_match: bool
    schema_valid: Optional[bool]
    intent_match: Optional[bool]
    action_match: Optional[bool]
    approval_match: Optional[bool]
    error_match: Optional[bool]
    points: float
    max_points: float
    notes: list[str]

    @property
    def ratio(self) -> float:
        if self.max_points == 0:
            return 0.0
        return self.points / self.max_points

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "status_match": self.status_match,
            "schema_valid": self.schema_valid,
            "intent_match": self.intent_match,
            "action_match": self.action_match,
            "approval_match": self.approval_match,
            "error_match": self.error_match,
            "points": self.points,
            "max_points": self.max_points,
            "ratio": round(self.ratio, 4),
            "notes": list(self.notes),
        }


def gold_from_dict(raw: dict[str, Any]) -> GoldCase:
    return GoldCase(
        ticket_id=str(raw["ticket_id"]),
        expected_status=str(raw["expected_status"]),
        intent=raw.get("intent"),
        proposed_action=raw.get("proposed_action"),
        expected_approval=raw.get("expected_approval"),
        expected_error_code=raw.get("expected_error_code"),
    )


def score_case(result: RunResult, gold: GoldCase) -> CaseScore:
    notes: list[str] = []
    points = 0.0
    max_points = 0.0

    max_points += 2
    status_match = result.status == gold.expected_status
    if status_match:
        points += 2
    else:
        notes.append(f"status {result.status} != {gold.expected_status}")

    # Only score schema validity when a completion was actually judged: a run
    # blocked before the provider call, or a provider-level fault, is N/A.
    schema_valid: Optional[bool] = None
    if result.output is not None:
        schema_valid = True
    elif result.error_code in CONTRACT_ERROR_CODES:
        schema_valid = False
    if schema_valid is not None:
        max_points += 1
        if schema_valid:
            points += 1
        else:
            notes.append(f"structured output invalid ({result.error_code})")

    intent_match: Optional[bool] = None
    if gold.intent is not None:
        max_points += 2
        actual = None if result.output is None else result.output.intent
        intent_match = actual == gold.intent
        if intent_match:
            points += 2
        else:
            notes.append(f"intent {actual} != {gold.intent}")

    action_match: Optional[bool] = None
    if gold.proposed_action is not None:
        max_points += 2
        actual_action = None if result.output is None else result.output.proposed_action
        action_match = actual_action == gold.proposed_action
        if action_match:
            points += 2
        else:
            notes.append(f"action {actual_action} != {gold.proposed_action}")

    approval_match: Optional[bool] = None
    if gold.expected_approval is not None:
        max_points += 1
        approval_match = result.approval == gold.expected_approval
        if approval_match:
            points += 1
        else:
            notes.append(f"approval {result.approval} != {gold.expected_approval}")

    error_match: Optional[bool] = None
    if gold.expected_error_code is not None:
        max_points += 1
        error_match = result.error_code == gold.expected_error_code
        if error_match:
            points += 1
        else:
            notes.append(f"error {result.error_code} != {gold.expected_error_code}")

    return CaseScore(
        ticket_id=gold.ticket_id,
        status_match=status_match,
        schema_valid=schema_valid,
        intent_match=intent_match,
        action_match=action_match,
        approval_match=approval_match,
        error_match=error_match,
        points=points,
        max_points=max_points,
        notes=notes,
    )


def _rate(matches: list[Optional[bool]]) -> Optional[float]:
    counted = [item for item in matches if item is not None]
    if not counted:
        return None
    return round(sum(1 for item in counted if item) / len(counted), 4)


def evaluate(results: list[RunResult], gold_cases: list[GoldCase]) -> dict[str, Any]:
    by_id = {item.ticket_id: item for item in results}
    scores: list[CaseScore] = []
    missing: list[str] = []
    for gold in gold_cases:
        result = by_id.get(gold.ticket_id)
        if result is None:
            missing.append(gold.ticket_id)
            continue
        scores.append(score_case(result, gold))

    retried = sum(1 for item in results if item.attempts > 1)
    blocked = sum(1 for item in results if item.status == "blocked")
    denied = sum(1 for item in results if item.status == "denied")
    pending = sum(1 for item in results if item.status == "pending_approval")
    cached = sum(1 for item in results if item.cached)

    total_points = sum(item.points for item in scores)
    total_max = sum(item.max_points for item in scores)
    report = {
        "cases": len(scores),
        "missing_results": missing,
        "mean_score": round(total_points / total_max, 4) if total_max else 0.0,
        "status_accuracy": _rate([item.status_match for item in scores]),
        "intent_accuracy": _rate([item.intent_match for item in scores]),
        "action_accuracy": _rate([item.action_match for item in scores]),
        "approval_accuracy": _rate([item.approval_match for item in scores]),
        "schema_validity_rate": _rate([item.schema_valid for item in scores]),
        "retry_rate": round(retried / len(results), 4) if results else 0.0,
        "blocked_rate": round(blocked / len(results), 4) if results else 0.0,
        "denied_rate": round(denied / len(results), 4) if results else 0.0,
        "pending_approval_rate": round(pending / len(results), 4) if results else 0.0,
        "cached_rate": round(cached / len(results), 4) if results else 0.0,
        "scores": [item.to_dict() for item in scores],
    }
    return report


def load_tickets(rows: list[dict[str, Any]]) -> list[Ticket]:
    return [ticket_from_dict(row) for row in rows]


def load_gold(rows: list[dict[str, Any]]) -> list[GoldCase]:
    return [gold_from_dict(row) for row in rows]
