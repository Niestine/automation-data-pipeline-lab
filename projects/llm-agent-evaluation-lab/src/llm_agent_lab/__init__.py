"""Provider-neutral LLM agent evaluation and guardrails lab."""

from .evaluation import GoldCase, evaluate
from .models import CONTRACT_VERSION, Ticket
from .orchestrator import AgentOrchestrator
from .provider import FakeProvider, HeuristicProvider

__all__ = [
    "CONTRACT_VERSION",
    "AgentOrchestrator",
    "FakeProvider",
    "GoldCase",
    "HeuristicProvider",
    "Ticket",
    "evaluate",
]
