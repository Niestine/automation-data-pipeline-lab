"""Classification values shared by the harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SideResult:
    """One parser's isolated or in-process result."""

    status: str
    value: object
    exc_type: str | None
    truncated: bool


@dataclass(frozen=True)
class Outcome:
    """Paired outcome for one blob."""

    kind: str
    side: str | None
    legacy: SideResult
    hardened: SideResult
    timeout_s: float | None
    truncated: bool


PRIORITY = {"CRASH": 0, "HANG": 1, "DISAGREE": 2, "AGREE": 3}

DISPOSITIONS = (
    "bug_new",
    "bug_legacy",
    "accept_compat",
    "accept_spec",
    "malformed_probe",
)
