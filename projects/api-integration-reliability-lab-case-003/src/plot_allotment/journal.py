"""In-memory fault journal. Each recovery branch records a decision."""

from __future__ import annotations


class Journal:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, fault: str, decision: str, **extra: object) -> None:
        if not fault or not decision:
            raise ValueError("fault and decision are required")
        event = {"fault": fault, "decision": decision}
        event.update(extra)
        self.events.append(event)

    def faults(self) -> list[str]:
        return [str(event["fault"]) for event in self.events]

    def has(self, fault: str, decision: str) -> bool:
        return any(
            event["fault"] == fault and event["decision"] == decision
            for event in self.events
        )
