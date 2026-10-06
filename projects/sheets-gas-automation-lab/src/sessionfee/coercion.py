"""Local RAW and USER_ENTERED storage stub.

RAW keeps the entered value. USER_ENTERED is a stand-in for the Sheets UI
parse: formula text becomes a formula marker, an ISO date becomes a serial,
and a numeric-looking string becomes a number (leading zeros drop).
This is not a measurement of the Sheets UI parser.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from sessionfee.canonical import decimal_string
from sessionfee.dateserial import to_serial

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


def store_value(value: Any, value_input_option: str) -> dict[str, Any]:
    if value_input_option == "RAW":
        return _store_raw(value)
    if value_input_option == "USER_ENTERED":
        return _store_user_entered(value)
    raise ValueError(f"unsupported valueInputOption {value_input_option}")


def _store_raw(value: Any) -> dict[str, Any]:
    if isinstance(value, Decimal):
        return {"input": "RAW", "kind": "number", "value": decimal_string(value)}
    if isinstance(value, bool):
        return {"input": "RAW", "kind": "text", "value": "TRUE" if value else "FALSE"}
    return {"input": "RAW", "kind": "text", "value": str(value)}


def _store_user_entered(value: Any) -> dict[str, Any]:
    if isinstance(value, Decimal):
        return {"input": "USER_ENTERED", "kind": "number", "value": decimal_string(value)}
    if not isinstance(value, str):
        return {"input": "USER_ENTERED", "kind": "text", "value": str(value)}
    if value.startswith("="):
        return {"input": "USER_ENTERED", "kind": "formula", "value": value}
    if _ISO_DATE.fullmatch(value):
        year, month, day = (int(part) for part in value.split("-"))
        serial = decimal_string(to_serial(datetime(year, month, day)))
        return {"input": "USER_ENTERED", "kind": "serial", "value": serial}
    if _NUMBER.fullmatch(value):
        return {
            "input": "USER_ENTERED",
            "kind": "number",
            "value": decimal_string(Decimal(value)),
        }
    return {"input": "USER_ENTERED", "kind": "text", "value": value}
