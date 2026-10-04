"""Gold-set scoring for tool-routing runs: set overlap, sequence, and disposition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .models import RunResult, WorkPacket, packet_from_dict


CONTRACT_ERROR_CODES = frozenset({"parse_error", "schema_error"})


@dataclass(frozen=True)
class GoldCase:
    packet_id: str
    expected_status: str
    goal_kind: Optional[str] = None
    expected_tools: Optional[list[str]] = None
    expected_error_code: Optional[str] = None


@dataclass
class CaseScore:
    packet_id: str
    status_match: bool
    schema_valid: Optional[bool]
    goal_kind_match: Optional[bool]
    sequence_match: Optional[bool]
    tool_precision: Optional[float]
    tool_recall: Optional[float]
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
            "packet_id": self.packet_id,
            "status_match": self.status_match,
            "schema_valid": self.schema_valid,
            "goal_kind_match": self.goal_kind_match,
            "sequence_match": self.sequence_match,
            "tool_precision": self.tool_precision,
            "tool_recall": self.tool_recall,
            "error_match": self.error_match,
            "points": self.points,
            "max_points": self.max_points,
            "ratio": round(self.ratio, 4),
            "notes": list(self.notes),
        }


def gold_from_dict(raw: dict[str, Any]) -> GoldCase:
    tools = raw.get("expected_tools")
    return GoldCase(
        packet_id=str(raw["packet_id"]),
        expected_status=str(raw["expected_status"]),
        goal_kind=raw.get("goal_kind"),
        expected_tools=None if tools is None else [str(item) for item in tools],
        expected_error_code=raw.get("expected_error_code"),
    )


def _overlap(predicted: list[str], gold: list[str]) -> tuple[float, float]:
    pred_set = set(predicted)
    gold_set = set(gold)
    if not pred_set:
        precision = 1.0 if not gold_set else 0.0
    else:
        precision = len(pred_set & gold_set) / len(pred_set)
    if not gold_set:
        recall = 1.0 if not pred_set else 0.0
    else:
        recall = len(pred_set & gold_set) / len(gold_set)
    return precision, recall


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

    schema_valid: Optional[bool] = None
    if result.plan is not None:
        schema_valid = True
    elif result.error_code in CONTRACT_ERROR_CODES:
        schema_valid = False
    if schema_valid is not None:
        max_points += 1
        if schema_valid:
            points += 1
        else:
            notes.append(f"structured plan invalid ({result.error_code})")

    goal_kind_match: Optional[bool] = None
    if gold.goal_kind is not None:
        max_points += 2
        actual = None if result.plan is None else result.plan.goal_kind
        goal_kind_match = actual == gold.goal_kind
        if goal_kind_match:
            points += 2
        else:
            notes.append(f"goal_kind {actual} != {gold.goal_kind}")

    sequence_match: Optional[bool] = None
    tool_precision: Optional[float] = None
    tool_recall: Optional[float] = None
    if gold.expected_tools is not None:
        predicted = [] if result.plan is None else result.plan.tool_names()
        tool_precision, tool_recall = _overlap(predicted, gold.expected_tools)
        max_points += 1
        if tool_precision == 1.0:
            points += 1
        else:
            notes.append(f"tool precision {tool_precision:.2f}")
        max_points += 1
        if tool_recall == 1.0:
            points += 1
        else:
            notes.append(f"tool recall {tool_recall:.2f}")
        max_points += 2
        sequence_match = predicted == gold.expected_tools
        if sequence_match:
            points += 2
        else:
            notes.append(f"sequence {predicted} != {gold.expected_tools}")

    error_match: Optional[bool] = None
    if gold.expected_error_code is not None:
        max_points += 1
        error_match = result.error_code == gold.expected_error_code
        if error_match:
            points += 1
        else:
            notes.append(f"error {result.error_code} != {gold.expected_error_code}")

    return CaseScore(
        packet_id=gold.packet_id,
        status_match=status_match,
        schema_valid=schema_valid,
        goal_kind_match=goal_kind_match,
        sequence_match=sequence_match,
        tool_precision=None if tool_precision is None else round(tool_precision, 4),
        tool_recall=None if tool_recall is None else round(tool_recall, 4),
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


def _mean(values: list[Optional[float]]) -> Optional[float]:
    counted = [item for item in values if item is not None]
    if not counted:
        return None
    return round(sum(counted) / len(counted), 4)


def evaluate(results: list[RunResult], gold_cases: list[GoldCase]) -> dict[str, Any]:
    by_id = {item.packet_id: item for item in results}
    scores: list[CaseScore] = []
    missing: list[str] = []
    for gold in gold_cases:
        result = by_id.get(gold.packet_id)
        if result is None:
            missing.append(gold.packet_id)
            continue
        scores.append(score_case(result, gold))

    retried = sum(1 for item in results if item.attempts > 1)
    blocked = sum(1 for item in results if item.status == "blocked")
    denied = sum(1 for item in results if item.status == "denied")
    pending = sum(1 for item in results if item.status == "pending_approval")
    cached = sum(1 for item in results if item.cached)
    budget_ok = [
        item
        for item in results
        if item.plan is None or item.tokens_planned <= item.token_budget
    ]

    total_points = sum(item.points for item in scores)
    total_max = sum(item.max_points for item in scores)
    report = {
        "cases": len(scores),
        "missing_results": missing,
        "mean_score": round(total_points / total_max, 4) if total_max else 0.0,
        "status_accuracy": _rate([item.status_match for item in scores]),
        "goal_kind_accuracy": _rate([item.goal_kind_match for item in scores]),
        "sequence_accuracy": _rate([item.sequence_match for item in scores]),
        "tool_precision": _mean([item.tool_precision for item in scores]),
        "tool_recall": _mean([item.tool_recall for item in scores]),
        "schema_validity_rate": _rate([item.schema_valid for item in scores]),
        "retry_rate": round(retried / len(results), 4) if results else 0.0,
        "blocked_rate": round(blocked / len(results), 4) if results else 0.0,
        "denied_rate": round(denied / len(results), 4) if results else 0.0,
        "pending_approval_rate": round(pending / len(results), 4) if results else 0.0,
        "cached_rate": round(cached / len(results), 4) if results else 0.0,
        "budget_ok_rate": round(len(budget_ok) / len(results), 4) if results else 0.0,
        "scores": [item.to_dict() for item in scores],
    }
    return report


def load_packets(rows: list[dict[str, Any]]) -> list[WorkPacket]:
    return [packet_from_dict(row) for row in rows]


def load_gold(rows: list[dict[str, Any]]) -> list[GoldCase]:
    return [gold_from_dict(row) for row in rows]
