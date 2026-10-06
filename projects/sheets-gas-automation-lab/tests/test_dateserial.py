"""SERIAL_NUMBER examples and the derived non-leap March 1900 step."""

from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

import support  # noqa: F401

from sessionfee.dateserial import leap_year_bug_serial, to_serial


class DateSerialTests(unittest.TestCase):
    def test_serial_1900_01_01_noon_is_2_5(self) -> None:
        self.assertEqual(to_serial(datetime(1900, 1, 1, 12, 0)), Decimal("2.5"))

    def test_serial_1900_02_01_15_00_is_33_625(self) -> None:
        self.assertEqual(to_serial(datetime(1900, 2, 1, 15, 0)), Decimal("33.625"))

    def test_derived_1900_03_01_is_61_not_printed_by_google(self) -> None:
        february_28 = to_serial(datetime(1900, 2, 28))
        march_1 = to_serial(datetime(1900, 3, 1))
        self.assertEqual(march_1 - february_28, Decimal("1"))
        self.assertEqual(march_1, Decimal("61"))

    def test_leap_year_bug_yields_62_for_derived_date(self) -> None:
        bug = leap_year_bug_serial(datetime(1900, 3, 1))
        self.assertEqual(bug, Decimal("62"))
        self.assertNotEqual(bug, to_serial(datetime(1900, 3, 1)))
        self.assertEqual(leap_year_bug_serial(datetime(1900, 1, 1, 12, 0)), Decimal("2.5"))
        self.assertEqual(leap_year_bug_serial(datetime(1900, 2, 1, 15, 0)), Decimal("33.625"))


if __name__ == "__main__":
    unittest.main()
