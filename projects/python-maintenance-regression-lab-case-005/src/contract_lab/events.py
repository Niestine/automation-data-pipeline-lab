"""Structured maintenance events. Tests assert these fields, not log text."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Event:
    kind: str
    channel: str | None = None
    operation: str | None = None
    element_pointer: str | None = None
    replacement: str | None = None
    deprecation_at: str | None = None
    sunset_at: str | None = None
    phase: str | None = None
    status: int | None = None
    target: str | None = None
    scope_uri: str | None = None
    applied_uri: str | None = None
    inherited: bool = False
    transport_error: str | None = None
    reader: str | None = None
    instance_location: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)
