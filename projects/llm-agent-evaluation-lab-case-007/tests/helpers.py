"""Shared constructors for the curb-permit regression tests."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from curbgate.models import RunManifest  # noqa: E402

EXAMPLES = PROJECT / "examples"


def manifest(**overrides) -> RunManifest:
    raw = {
        "manifest_id": "baseline",
        "provider_name": "fake-curb",
        "request_model": "curb-script-1",
        "response_model": "curb-script-1",
        "temperature": 0.0,
        "top_p": 1.0,
        "seed_schedule": [100],
        "epochs": 1,
        "reducer": "mean",
        "pass_k": 1,
        "headline_metric": "task_correct",
        "headline_capability": "permit_decision",
        "max_tokens": 256,
        "prompt_name": "clerk.issue",
        "prompt_version": "1.0.0",
        "prompt_templates": {
            "clerk.direct": "Decide the curb permit. Put the reason before the answer.",
            "negation.v1": "Decide whether the curb report is clear.",
        },
        "schema_id": "curb.reason_v1",
        "schema_key_order": ["reason", "answer"],
        "declared_change": "none",
        "alpha": 0.05,
        "power": 0.8,
        "delta": 0.5,
        "harness_error_budget": 0.0,
        "claimed_risk_tags": ["confabulation"],
        "change_kind": "improvement",
        "output_type": "json",
        "tool_policy_version": "allowlist.v1",
        "max_actions": 8,
        "max_retries": 2,
        "reasoning_task_min": 0.5,
        "classification_task_min": 0.5,
    }
    raw.update(overrides)
    return RunManifest.from_dict(raw)
