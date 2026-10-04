"""Frozen SLIP v1 parser.

The policy in this module is the compatibility baseline. Maintenance
edits belong in ``hardened.py``. Do not change the flags below to make
a test pass; record the decision on the ledger instead.
"""

from __future__ import annotations

from slip_lab.engine import Policy, scan

POLICY = Policy(
    name="legacy",
    doubled_quotes=False,
    drop_trailing_empty=True,
    nul_mode="crash",
    cr_mode="crash",
    hang_seconds=30,
    strip_hash_lines=False,
    reject_hang_token=False,
)


def parse(blob: bytes) -> tuple:
    return scan(blob, POLICY)
