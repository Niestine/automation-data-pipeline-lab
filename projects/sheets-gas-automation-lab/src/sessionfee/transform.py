"""Session-fee rules kept in ordinary functions.

The sheet does not host these steps. Row amount is hours times the declared
JPY/hour rate. The week total adds those JPY amounts. The other two totals
are ablation writers: one copies the first row, and one drops the last row.
"""

from __future__ import annotations

from decimal import Decimal

from sessionfee.canonical import SessionInput
from sessionfee.dimensions import HOUR, JPY, JPY_PER_HOUR, DimensionMismatch, Quantity, add, multiply
from sessionfee.schema import SessionRow


def session_amount(hours: Quantity, rate: Quantity) -> Quantity:
    product = multiply(hours, rate)
    if product.dimension != JPY:
        raise DimensionMismatch(
            f"hours times rate produced {product.dimension.label()}, expected JPY"
        )
    return product


def group_total(amounts: list[Quantity]) -> Quantity:
    total = Quantity(Decimal("0"), JPY)
    for amount in amounts:
        total = add(total, amount)
    return total


def first_row_only_total(amounts: list[Quantity]) -> Quantity:
    """Ablation writer: the stored total is the first row and omits the rest."""

    if not amounts:
        raise ValueError("first_row_only_total requires at least one amount")
    return amounts[0]


def dropping_total(amounts: list[Quantity]) -> Quantity:
    """Ablation writer that drops the last amount. Used as a shared blind oracle."""

    if not amounts:
        return Quantity(Decimal("0"), JPY)
    if len(amounts) == 1:
        return amounts[0]
    return group_total(amounts[:-1])


def build_row(session: SessionInput) -> SessionRow:
    hours = Quantity(session.hours, HOUR)
    rate = Quantity(session.rate_jpy_per_hour, JPY_PER_HOUR)
    amount = session_amount(hours, rate)
    return SessionRow(
        operation_id=session.operation_id,
        session_date=session.session_date,
        desk_code=session.desk_code,
        hours=hours,
        rate=rate,
        amount=amount,
    )
