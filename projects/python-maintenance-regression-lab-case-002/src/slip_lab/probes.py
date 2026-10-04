"""Planted parsers for the differential harness.

These functions exist so a test can name a crash, a process exit, or a
value outside the dialect contract without editing the frozen legacy
policy. The planted hang is the legacy ``__HANG__`` token.
"""

from __future__ import annotations

import os


def crash_always(blob: bytes) -> tuple:
    raise RuntimeError("planted-crash")


def exit_always(blob: bytes) -> tuple:
    os._exit(3)


def invalid_result(blob: bytes) -> tuple:
    return ("not-a-slip-value",)
