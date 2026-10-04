"""Pair the frozen parser with the maintained parser.

Outcome priority is CRASH, then HANG, then DISAGREE, then AGREE.
Returned error codes are ordinary results. Diagnostic text is not
compared. A truncated payload is withheld from equality. A returned
value outside the dialect contract is a crash of that side
(``InvalidResult``), the same as an uncaught exception.
"""

from __future__ import annotations

from slip_lab.dialect import canonical, same
from slip_lab.isolate import run_targets
from slip_lab.model import Outcome, SideResult


def call_parser(fn, blob: bytes) -> SideResult:
    try:
        value = fn(blob)
    except Exception as exc:
        return SideResult("crash", None, type(exc).__name__, False)
    return SideResult("ok", value, None, False)


def _checked(side: SideResult) -> SideResult:
    if side.status != "ok" or side.truncated:
        return side
    try:
        canonical(side.value)
    except TypeError:
        return SideResult("crash", None, "InvalidResult", False)
    return side


def combine(legacy: SideResult, hardened: SideResult, timeout_s: float | None) -> Outcome:
    legacy = _checked(legacy)
    hardened = _checked(hardened)
    truncated = bool(legacy.truncated or hardened.truncated)
    if legacy.status == "crash" or hardened.status == "crash":
        if legacy.status == "crash" and hardened.status == "crash":
            side = "both"
        elif legacy.status == "crash":
            side = "legacy"
        else:
            side = "hardened"
        return Outcome("CRASH", side, legacy, hardened, timeout_s, truncated)
    if legacy.status == "hang" or hardened.status == "hang":
        if legacy.status == "hang" and hardened.status == "hang":
            side = "both"
        elif legacy.status == "hang":
            side = "legacy"
        else:
            side = "hardened"
        return Outcome("HANG", side, legacy, hardened, timeout_s, truncated)
    if truncated:
        return Outcome("DISAGREE", "both", legacy, hardened, timeout_s, True)
    if same(legacy.value, hardened.value):
        return Outcome("AGREE", None, legacy, hardened, timeout_s, False)
    return Outcome("DISAGREE", "both", legacy, hardened, timeout_s, False)


def classify_inprocess(blob: bytes, legacy_fn=None, hardened_fn=None, timeout_s: float | None = None) -> Outcome:
    if legacy_fn is None:
        from slip_lab.legacy import parse as legacy_fn
    if hardened_fn is None:
        from slip_lab.hardened import parse as hardened_fn
    return combine(call_parser(legacy_fn, blob), call_parser(hardened_fn, blob), timeout_s)


def classify_isolated(
    blob: bytes,
    *,
    legacy_target: str = "slip_lab.legacy:parse",
    hardened_target: str = "slip_lab.hardened:parse",
    timeout: float = 5.0,
    max_output_bytes: int = 1_000_000,
) -> Outcome:
    legacy, hardened = run_targets(legacy_target, hardened_target, blob, timeout, max_output_bytes)
    return combine(legacy, hardened, timeout)
