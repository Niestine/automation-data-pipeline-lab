"""Lotus-style datetime serials used by Sheets' SERIAL_NUMBER rendering.

The public enum text defines the epoch as days since 30 December 1899 and
says the format treats 1900 as not a leap year. It prints two examples:
1900-01-01 at noon is 2.5, and 1900-02-01 at 15:00 is 33.625.

1900-03-01 as 61 is derived here from that epoch and a 28-day February 1900.
It is not a number printed on the datetime page. ``leap_year_bug_serial`` is
a local control that inserts a phantom day on and after 1900-03-01.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

EPOCH = datetime(1899, 12, 30)
_DAY_SECONDS = Decimal(86400)


def to_serial(moment: datetime) -> Decimal:
    """Return the SERIAL_NUMBER for a naive civil datetime."""

    if moment.tzinfo is not None:
        raise ValueError("serial conversion expects a naive civil datetime")
    delta = moment - EPOCH
    if delta.microseconds:
        raise ValueError("serial conversion in this lab is whole-second")
    total_seconds = Decimal(delta.days) * _DAY_SECONDS + Decimal(delta.seconds)
    return total_seconds / _DAY_SECONDS


def leap_year_bug_serial(moment: datetime) -> Decimal:
    """Control encoding that treats 1900-02-29 as if it had existed.

    This is not Google's SERIAL_NUMBER. The published text says 1900 is not
    a leap year. The phantom day is added only on and after 1900-03-01 so
    the two printed 1900-01 and 1900-02 examples stay unchanged.
    """

    serial = to_serial(moment)
    if moment >= datetime(1900, 3, 1):
        return serial + Decimal(1)
    return serial
