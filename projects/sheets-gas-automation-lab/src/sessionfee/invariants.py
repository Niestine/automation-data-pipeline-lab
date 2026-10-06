"""Independent bottom-line check.

The expected total and row count are literals supplied by the caller, usually
from a fixture file. This module does not compute the fee total itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class InvariantResult:
    passed: bool
    reasons: tuple[str, ...]


def check_invariant(
    *,
    written_total: Decimal,
    expected_total: Decimal,
    written_row_count: int,
    expected_row_count: int,
) -> InvariantResult:
    """Compare a stored aggregate with fixture literals.

    ``written_total`` is whatever the sheet or writer stored. ``expected_total``
    must already be the independent number. Passing both through the same
    total function hides a shared omission.
    """

    reasons: list[str] = []
    if written_total != expected_total:
        reasons.append("total")
    if written_row_count != expected_row_count:
        reasons.append("row_count")
    return InvariantResult(not reasons, tuple(reasons))
