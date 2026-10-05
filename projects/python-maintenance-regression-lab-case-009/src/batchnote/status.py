"""Process statuses this program is allowed to return."""

from __future__ import annotations

import warnings

OK = 0
FAILURE = 1
USAGE = 2

# None blocks a status or default-dialect change. A real deprecation sets
# this to the version where the new behavior would become the default.
USAGE_REMOVAL = None

# Historic sysexits.h integers. They are documented for callers who still
# know the names, and they are never returned.
HISTORIC_SYSEXITS = {
    "EX_USAGE": 64,
    "EX_DATAERR": 65,
    "EX_NOINPUT": 66,
    "EX_UNAVAILABLE": 69,
    "EX_SOFTWARE": 70,
    "EX_OSERR": 71,
    "EX_OSFILE": 72,
    "EX_CANTCREAT": 73,
    "EX_IOERR": 74,
    "EX_CONFIG": 78,
}


def finalize(failure_count: int) -> int:
    """Map a recoverable-failure count onto OK or FAILURE.

    An 8-bit wait status would report 256 errors as success.
    """
    if isinstance(failure_count, bool) or not isinstance(failure_count, int):
        raise ValueError("failure_count")
    if failure_count < 0:
        raise ValueError("failure_count")
    return OK if failure_count == 0 else FAILURE


def usage_status() -> int:
    """Return the usage status. Warn only after a removal version is declared."""
    removal = USAGE_REMOVAL
    if removal is not None:
        warnings.warn(
            f"usage status {USAGE} remains the default until removal in {removal}",
            DeprecationWarning,
            stacklevel=2,
        )
    return USAGE
