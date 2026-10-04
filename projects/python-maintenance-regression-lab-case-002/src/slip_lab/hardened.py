"""Maintained SLIP parser.

Open defect: a record whose raw text starts with ``#`` is dropped.
That token is a legal lane id in the dialect. The ledger pins the
current failure as ``bug_new`` until a fix moves the row.
"""

from __future__ import annotations

from slip_lab.engine import Policy, scan

POLICY = Policy(
    name="hardened",
    doubled_quotes=True,
    drop_trailing_empty=False,
    nul_mode="error",
    cr_mode="error",
    hang_seconds=0,
    strip_hash_lines=True,
    reject_hang_token=True,
)


def parse(blob: bytes) -> tuple:
    return scan(blob, POLICY)
