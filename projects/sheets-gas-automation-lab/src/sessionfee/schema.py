"""Per-cell schema for a session-fee row.

A row passes when hours, rate, and amount have the declared dimension and
sign, and the row amount equals hours times rate. This check does not look
at the week total.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sessionfee.canonical import parse_iso_date
from sessionfee.dimensions import (
    HOUR,
    JPY,
    JPY_PER_HOUR,
    DimensionMismatch,
    Quantity,
    multiply,
)

_NUMBER = re.compile(r"-?\d+(\.\d+)?")


@dataclass(frozen=True)
class TextCell:
    value: str


@dataclass(frozen=True)
class SessionRow:
    operation_id: str
    session_date: str
    desk_code: str
    hours: Quantity
    rate: Quantity
    amount: Quantity | TextCell


@dataclass(frozen=True)
class SchemaResult:
    passed: bool
    errors: tuple[str, ...]


def _number(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str) and _NUMBER.fullmatch(value):
        return Decimal(value)
    return None


def row_from_mapping(raw: dict[str, Any]) -> SessionRow:
    hours_value = _number(raw.get("hours"))
    rate_value = _number(raw.get("rate_jpy_per_hour"))
    if hours_value is None or rate_value is None:
        raise ValueError("hours and rate_jpy_per_hour must be decimal strings")
    amount_raw = raw.get("amount_jpy")
    amount_value = _number(amount_raw)
    amount: Quantity | TextCell
    if amount_value is None:
        amount = TextCell("" if amount_raw is None else str(amount_raw))
    else:
        amount = Quantity(amount_value, JPY)
    return SessionRow(
        operation_id="" if raw.get("operation_id") is None else str(raw.get("operation_id")),
        session_date="" if raw.get("session_date") is None else str(raw.get("session_date")),
        desk_code="" if raw.get("desk_code") is None else str(raw.get("desk_code")),
        hours=Quantity(hours_value, HOUR),
        rate=Quantity(rate_value, JPY_PER_HOUR),
        amount=amount,
    )


def check_row(row: SessionRow) -> SchemaResult:
    errors: list[str] = []
    if not row.operation_id.strip():
        errors.append("operation_id")
    if not row.desk_code.strip():
        errors.append("desk_code")
    try:
        parse_iso_date(row.session_date)
    except ValueError:
        errors.append("session_date")
    if row.hours.dimension != HOUR:
        errors.append("hours_dimension")
    if row.hours.value < 0:
        errors.append("hours_sign")
    if row.rate.dimension != JPY_PER_HOUR:
        errors.append("rate_dimension")
    if row.rate.value < 0:
        errors.append("rate_sign")
    if not isinstance(row.amount, Quantity):
        errors.append("amount_type")
        return SchemaResult(False, tuple(errors))
    if row.amount.dimension != JPY:
        errors.append("amount_dimension")
        return SchemaResult(False, tuple(errors))
    try:
        product = multiply(row.hours, row.rate)
    except DimensionMismatch:
        errors.append("amount_product")
        return SchemaResult(False, tuple(errors))
    if product.dimension != JPY or product.value != row.amount.value:
        errors.append("amount_product")
    return SchemaResult(not errors, tuple(errors))


def check_rows(rows: list[SessionRow]) -> SchemaResult:
    errors: list[str] = []
    for index, row in enumerate(rows):
        result = check_row(row)
        for error in result.errors:
            errors.append(f"row {index}: {error}")
    return SchemaResult(not errors, tuple(errors))


def dimensions_consistent(row: SessionRow) -> bool:
    """Dimension unification only. A wrong week total still passes."""

    if row.hours.dimension != HOUR or row.rate.dimension != JPY_PER_HOUR:
        return False
    if not isinstance(row.amount, Quantity) or row.amount.dimension != JPY:
        return False
    try:
        product = multiply(row.hours, row.rate)
    except DimensionMismatch:
        return False
    return product.dimension == JPY
