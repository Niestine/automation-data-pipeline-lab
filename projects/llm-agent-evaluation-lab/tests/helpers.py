"""Path bootstrap and factories for unittest modules."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from llm_agent_lab.models import Ticket  # noqa: E402
from llm_agent_lab.orchestrator import AgentOrchestrator  # noqa: E402
from llm_agent_lab.provider import FakeProvider  # noqa: E402
from llm_agent_lab.retry import RetryPolicy  # noqa: E402
from llm_agent_lab.store import RunStore  # noqa: E402
from llm_agent_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper  # noqa: E402
from llm_agent_lab.tools import RecordStore  # noqa: E402


def valid_output_dict(**overrides):
    payload = {
        "intent": "status_lookup",
        "confidence": 0.9,
        "entities": {"record_id": "ORD-100"},
        "proposed_action": "lookup_record",
        "action_args": {"record_id": "ORD-100"},
        "rationale": "lookup order status",
        "needs_human": False,
    }
    payload.update(overrides)
    return payload


def valid_output_json(**overrides) -> str:
    return json.dumps(valid_output_dict(**overrides))


def make_ticket(**overrides) -> Ticket:
    payload = {
        "ticket_id": "T-1001",
        "subject": "Status of ORD-100",
        "body": "What is the status of ORD-100? Lookup only, no edits.",
        "requester_role": "intern",
        "channel": "internal_ops",
    }
    payload.update(overrides)
    return Ticket(**payload)


def scripted_provider(ticket_id: str, steps, **other_scripts) -> FakeProvider:
    scripts = {ticket_id: steps}
    scripts.update(other_scripts)
    return FakeProvider(scripts)


def make_orchestrator(provider, *, records=None, policy=None, seed=7):
    clock = ManualClock()
    sleeper = RecordingSleeper(clock)
    logger = JsonLogger()
    orchestrator = AgentOrchestrator(
        provider,
        records=records if records is not None else RecordStore(),
        store=RunStore(),
        policy=policy if policy is not None else RetryPolicy(),
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        seed=seed,
    )
    return orchestrator, sleeper, logger
