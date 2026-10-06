"""Canonical session records and pre-coercion fingerprints.

The fingerprint is a SHA-256 checksum of the canonical JSON request:
sorted keys, decimal strings, ISO dates, and dimension tags. It is taken
before any USER_ENTERED stand-in rewrites a date into a serial.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sessionfee.dateserial import to_serial

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def decimal_string(value: Decimal | int | str) -> str:
    """Stable decimal text: 2.50 and 2.5 both become '2.5'."""

    number = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def parse_iso_date(value: str) -> datetime:
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise ValueError(f"session_date must be an ISO date, got {value!r}")
    year, month, day = (int(part) for part in value.split("-"))
    return datetime(year, month, day)


def operation_id_for(label: str) -> str:
    """Deterministic UUID-shaped id for synthetic fixtures."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, "sessionfee:" + label))


@dataclass(frozen=True)
class SessionInput:
    operation_id: str
    session_date: str
    desk_code: str
    hours: Decimal
    rate_jpy_per_hour: Decimal

    def canonical(self) -> dict[str, Any]:
        parse_iso_date(self.session_date)
        if not self.desk_code or not isinstance(self.desk_code, str):
            raise ValueError("desk_code is required")
        return {
            "desk_code": self.desk_code,
            "hours": {"dimension": "hour", "value": decimal_string(self.hours)},
            "operation_id": self.operation_id,
            "rate": {
                "dimension": "JPY/hour",
                "value": decimal_string(self.rate_jpy_per_hour),
            },
            "session_date": self.session_date,
        }


def fingerprint(record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def user_entered_stub(record: dict[str, Any]) -> dict[str, Any]:
    """Stand-in for USER_ENTERED date parsing. Not the Sheets UI parser.

    The archived enum says strings may be converted to numbers or dates.
    This stub rewrites an ISO date field into the lab's serial text and
    leaves every other field alone.
    """

    rewritten = json.loads(json.dumps(record))
    session_date = rewritten.get("session_date")
    if isinstance(session_date, str) and _ISO_DATE.fullmatch(session_date):
        moment = parse_iso_date(session_date)
        rewritten["session_date"] = decimal_string(to_serial(moment))
    return rewritten
