"""Path bootstrap and factories for unittest modules."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from brief_router_lab.models import WorkPacket  # noqa: E402
from brief_router_lab.orchestrator import BriefRouter  # noqa: E402
from brief_router_lab.provider import FakePlanner  # noqa: E402
from brief_router_lab.retry import CircuitBreaker, RetryPolicy  # noqa: E402
from brief_router_lab.store import RunStore, StepStore  # noqa: E402
from brief_router_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper  # noqa: E402
from brief_router_lab.workspace import BriefWorkspace, ToolFaults  # noqa: E402


def valid_plan_dict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "tool_plan_v1",
        "goal_kind": "lookup",
        "confidence": 0.91,
        "budget_tokens": 20,
        "needs_human": False,
        "steps": [
            {
                "id": "s1",
                "tool": "catalog.search",
                "args": {"query": "weekly recap", "limit": 3},
                "bind": {},
            }
        ],
        "rationale": "lookup catalog",
    }
    payload.update(overrides)
    return payload


def valid_plan_json(**overrides: Any) -> str:
    return json.dumps(valid_plan_dict(**overrides))


def make_packet(**overrides: Any) -> WorkPacket:
    payload = {
        "packet_id": "P-2001",
        "goal": "Search the catalog for weekly recap clips. Limit 3.",
        "operator_role": "viewer",
        "workspace": "public",
        "max_steps": 8,
        "token_budget": 200,
    }
    payload.update(overrides)
    return WorkPacket(**payload)


def scripted_planner(packet_id: str, steps: list[dict[str, Any]], **other_scripts: Any) -> FakePlanner:
    scripts = {packet_id: steps}
    scripts.update(other_scripts)
    return FakePlanner(scripts)


def make_router(
    provider: Any,
    *,
    workspace: BriefWorkspace | None = None,
    policy: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
    seed: int = 11,
) -> tuple[BriefRouter, RecordingSleeper, JsonLogger]:
    clock = ManualClock()
    sleeper = RecordingSleeper(clock)
    logger = JsonLogger()
    router = BriefRouter(
        provider,
        workspace=workspace if workspace is not None else BriefWorkspace(),
        store=RunStore(),
        steps=StepStore(),
        policy=policy if policy is not None else RetryPolicy(),
        breaker=breaker if breaker is not None else CircuitBreaker(),
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        seed=seed,
    )
    return router, sleeper, logger


def load_example(name: str) -> Any:
    with (EXAMPLES / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)
