"""Hold the LLM judge until a frozen rubric hash is the one in the gold file.

World-state oracles and the deterministic lints grade the suite. This module
does not call a model and does not simulate tool execution.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .errors import JudgeGateError

RUBRIC_LINES = (
    "mast-lint-v1",
    "disobey_task_spec: tool sequence differs from the task contract",
    "step_repetition: identical tool name and canonical arguments without a ledger replay mark",
    "premature_termination: run stopped before the golden writes",
    "no_verification: trace has no outbox comparison",
    "incorrect_verification: verification span passed while the world oracle failed",
)


def rubric_text() -> str:
    return "\n".join(RUBRIC_LINES) + "\n"


def rubric_sha256() -> str:
    return hashlib.sha256(rubric_text().encode("utf-8")).hexdigest()


def check_gate(gold: dict[str, Any]) -> dict[str, Any]:
    code_hash = rubric_sha256()
    file_hash = gold.get("rubric_sha256")
    agreement = gold.get("agreement")
    if file_hash != code_hash and not isinstance(agreement, (int, float)):
        raise JudgeGateError("rubric hash changed without a new agreement number")
    if file_hash != code_hash and gold.get("agreement_for_hash") != code_hash:
        raise JudgeGateError("agreement does not match the current rubric hash")
    return {"enabled": False, "reason": "llm_judge_held", "rubric_match": file_hash == code_hash}
