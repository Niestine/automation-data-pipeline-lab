"""Hand-placed mutants. Kill rate is reported apart from statement coverage.

None of these mutants is marked equivalent: each one is visible to the corpus oracle.
"""

from __future__ import annotations

import ast
import trace
from pathlib import Path

from .corpus import Case, call_case, legacy_expectation, outcome_matches, strict_expectation

MUTANTS: tuple[str, ...] = (
    "field_count_swap",
    "limit_boundary_swap",
    "drop_unclosed",
    "drop_doubled_quote",
    "semicolon_comma",
    "drop_ascii_restore",
)

# drop_ascii_restore lives in the legacy replacement wrapper. The others live in the strict recognizer.
PROFILE: dict[str, str] = {
    "field_count_swap": "hardened",
    "limit_boundary_swap": "hardened",
    "drop_unclosed": "hardened",
    "drop_doubled_quote": "hardened",
    "semicolon_comma": "hardened",
    "drop_ascii_restore": "legacy",
}

# A mutant is listed here only when no oracle input can observe it. The value is the reason.
EQUIVALENT: dict[str, str] = {}

_PACKAGE = Path(__file__).resolve().parent
_COVERED = ("recognize.py", "decode.py", "unparse.py")


def kills(case: Case, mutant: str) -> bool:
    profile = PROFILE[mutant]
    expected = strict_expectation(case) if profile == "hardened" else legacy_expectation(case)
    if expected is None:
        return False
    actual = call_case(case, profile, mutant)
    return not outcome_matches(actual, expected)


def kill_rate(cases: list[Case], skip: frozenset[str] = frozenset()) -> float:
    active = [name for name in MUTANTS if name not in EQUIVALENT]
    if not active:
        return 1.0
    killed = 0
    usable = [case for case in cases if case.name not in skip]
    for mutant in active:
        if any(kills(case, mutant) for case in usable):
            killed += 1
    return killed / len(active)


def survivor_names(cases: list[Case], skip: frozenset[str] = frozenset()) -> list[str]:
    usable = [case for case in cases if case.name not in skip]
    return [name for name in MUTANTS if name not in EQUIVALENT and not any(kills(case, name) for case in usable)]


def statement_coverage(callback) -> float:
    """Fraction of function-body statements executed by callback. Independent of the kill rate."""

    tracer = trace.Trace(count=1, trace=0)
    tracer.runfunc(callback)
    counts = tracer.results().counts
    hit: set[tuple[str, int]] = set()
    for (filename, lineno), times in counts.items():
        if times:
            hit.add((str(Path(filename).resolve()), lineno))
    total = 0
    seen = 0
    for name in _COVERED:
        path = _PACKAGE / name
        lines = _body_lines(path)
        total += len(lines)
        resolved = str(path.resolve())
        seen += sum(1 for lineno in lines if (resolved, lineno) in hit)
    if total == 0:
        return 0.0
    return seen / total


def _body_lines(path: Path) -> set[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.stmt) and child is not node:
                    lines.add(child.lineno)
    return lines
