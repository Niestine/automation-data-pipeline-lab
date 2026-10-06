"""Declared dimension unification, with a numeric baseline that ignores units."""

from __future__ import annotations

import unittest
from decimal import Decimal

import support  # noqa: F401

from sessionfee.dimensions import (
    CENT,
    DIMENSIONLESS,
    Dimension,
    HOUR,
    JPY,
    JPY_PER_HOUR,
    DimensionMismatch,
    Quantity,
    UndeclaredDimension,
    UnsupportedConversion,
    add,
    compare,
    dimension_for_column,
    fahrenheit_to_celsius,
    multiply,
    numeric_add,
)
from sessionfee.schema import SessionRow, TextCell, dimensions_consistent
from sessionfee.transform import session_amount


class DimensionTests(unittest.TestCase):
    def test_jpy_plus_jpy_passes(self) -> None:
        total = add(Quantity(Decimal("1000"), JPY), Quantity(Decimal("2500"), JPY))
        self.assertEqual(total.value, Decimal("3500"))
        self.assertEqual(total.dimension, JPY)

    def test_jpy_plus_hours_numeric_baseline_accepts_and_dimension_rejects(self) -> None:
        self.assertEqual(numeric_add(Decimal("1000"), Decimal("2")), Decimal("1002"))
        with self.assertRaises(DimensionMismatch):
            add(Quantity(Decimal("1000"), JPY), Quantity(Decimal("2"), HOUR))

    def test_hours_times_jpy_per_hour_yields_jpy(self) -> None:
        amount = session_amount(
            Quantity(Decimal("2.5"), HOUR),
            Quantity(Decimal("4000"), JPY_PER_HOUR),
        )
        self.assertEqual(amount.dimension, JPY)
        self.assertEqual(amount.value, Decimal("10000"))
        self.assertEqual(
            multiply(Quantity(Decimal("2"), HOUR), Quantity(Decimal("4000"), JPY_PER_HOUR)).dimension,
            JPY,
        )

    def test_jpy_greater_than_hours_fails(self) -> None:
        self.assertTrue(compare(Quantity(Decimal("3"), JPY), Quantity(Decimal("2"), JPY), ">"))
        with self.assertRaises(DimensionMismatch):
            compare(Quantity(Decimal("1000"), JPY), Quantity(Decimal("2"), HOUR), ">")

    def test_dimensionless_plus_hours_fails_and_dimensionless_plus_itself_passes(self) -> None:
        with self.assertRaises(DimensionMismatch):
            add(Quantity(Decimal("3"), DIMENSIONLESS), Quantity(Decimal("1.5"), HOUR))
        total = add(Quantity(Decimal("3"), DIMENSIONLESS), Quantity(Decimal("1"), DIMENSIONLESS))
        self.assertEqual(total.dimension, DIMENSIONLESS)
        self.assertEqual(total.value, Decimal("4"))

    def test_header_amount_does_not_become_jpy(self) -> None:
        with self.assertRaises(UndeclaredDimension):
            dimension_for_column("Amount")
        self.assertEqual(dimension_for_column("amount_jpy"), JPY)

    def test_fahrenheit_offset_conversion_unsupported(self) -> None:
        with self.assertRaises(UnsupportedConversion):
            fahrenheit_to_celsius(Decimal("68"))

    def test_different_money_factors_do_not_add(self) -> None:
        with self.assertRaises(DimensionMismatch):
            add(Quantity(Decimal("100"), JPY), Quantity(Decimal("100"), CENT))

    def test_equal_factors_written_differently_still_add(self) -> None:
        one_point_zero = Dimension.base("money", "1.0")
        self.assertEqual(one_point_zero, JPY)
        total = add(Quantity(Decimal("100"), JPY), Quantity(Decimal("5"), one_point_zero))
        self.assertEqual(total.value, Decimal("105"))
        self.assertEqual(Dimension.base("money", "0.010"), CENT)

    def test_jpy_times_jpy_is_not_rejected_by_the_factor_model(self) -> None:
        product = multiply(Quantity(Decimal("2"), JPY), Quantity(Decimal("3"), JPY))
        self.assertEqual(product.value, Decimal("6"))
        self.assertNotEqual(product.dimension, JPY)

    def test_wrong_amount_dimension_fails_unification(self) -> None:
        row = SessionRow(
            operation_id="op",
            session_date="2026-10-05",
            desk_code="DESK-NORTH",
            hours=Quantity(Decimal("1"), HOUR),
            rate=Quantity(Decimal("4000"), JPY_PER_HOUR),
            amount=Quantity(Decimal("4000"), HOUR),
        )
        self.assertFalse(dimensions_consistent(row))
        self.assertFalse(dimensions_consistent(
            SessionRow(
                operation_id="op",
                session_date="2026-10-05",
                desk_code="DESK-NORTH",
                hours=Quantity(Decimal("1"), HOUR),
                rate=Quantity(Decimal("4000"), JPY_PER_HOUR),
                amount=TextCell("TBD"),
            )
        ))


if __name__ == "__main__":
    unittest.main()
