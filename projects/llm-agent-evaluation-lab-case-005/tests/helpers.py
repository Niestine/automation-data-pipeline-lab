"""Shared paths for the Kilnline unittest suite."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
SRC = PROJECT / "src"
EXAMPLES = PROJECT / "examples"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_OUTCOME = None


def outcome():
    """Run the shipped cassettes once per process."""
    global _OUTCOME
    if _OUTCOME is None:
        from repair_gate.suite import execute_suite

        _OUTCOME = execute_suite(EXAMPLES, EXAMPLES / "gold.json", None, write_manifest=False)
    return _OUTCOME


def result(case_id: str):
    for item in outcome()["catalog"]["results"]:
        if item.case_id == case_id:
            return item
    raise KeyError(case_id)
