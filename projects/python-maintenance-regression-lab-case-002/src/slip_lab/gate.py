"""Disposition gate. A fresh classification has to match the ledger."""

from __future__ import annotations

from slip_lab.dialect import same
from slip_lab.errors import GateError
from slip_lab.model import DISPOSITIONS, Outcome


def _problems(row: dict, outcome: Outcome) -> list[str]:
    problems = []
    label = row.get("disposition")
    if outcome.kind != "AGREE" and not label:
        problems.append("missing disposition")
    if label is not None and label not in DISPOSITIONS:
        problems.append("unknown disposition")
    if outcome.kind != row.get("outcome") or outcome.side != row.get("side"):
        problems.append(
            f"outcome drift stored={row.get('outcome')}/{row.get('side')} fresh={outcome.kind}/{outcome.side}"
        )
    if outcome.kind in {"CRASH", "HANG"} and label in {"accept_compat", "accept_spec"}:
        problems.append(f"{label} forbids {outcome.kind}")
    if label == "accept_compat" and outcome.kind != "AGREE":
        problems.append("accept_compat drift")
    hardened_value = outcome.hardened.value if outcome.hardened.status == "ok" else None
    if label == "accept_spec":
        if hardened_value is None or not same(hardened_value, _as_tuple(row.get("oracle"))):
            problems.append("accept_spec oracle mismatch")
    elif row.get("hardened_value") is not None:
        if hardened_value is None or not same(hardened_value, _as_tuple(row.get("hardened_value"))):
            problems.append("hardened value drift")
    if row.get("exc_type") and outcome.kind == "CRASH":
        seen = outcome.legacy.exc_type or outcome.hardened.exc_type
        if seen != row["exc_type"]:
            problems.append("exception type drift")
    return problems


def _as_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(_as_tuple(item) for item in value)
    return value


def evaluate(row: dict, outcome: Outcome) -> list[str]:
    return _problems(row, outcome)


def require(row: dict, outcome: Outcome) -> None:
    problems = evaluate(row, outcome)
    if problems:
        raise GateError("; ".join(problems))
