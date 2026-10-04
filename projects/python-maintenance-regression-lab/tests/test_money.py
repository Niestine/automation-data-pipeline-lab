import unittest

import helpers  # noqa: F401

from maintenance_lab.errors import ValidationError
from maintenance_lab.money import format_minor, parse_price


class MoneyTests(unittest.TestCase):
    def test_usd_decimal(self):
        self.assertEqual(parse_price("19.99", "USD"), 1999)
        self.assertEqual(parse_price("0.10", "USD"), 10)
        self.assertEqual(parse_price("1.13", "USD"), 113)
        self.assertEqual(parse_price("$21.50", "USD"), 2150)

    def test_thousands_separator(self):
        self.assertEqual(parse_price("1,234.56", "USD"), 123456)

    def test_jpy_whole_yen_and_thousands(self):
        self.assertEqual(parse_price("4800", "JPY"), 4800)
        self.assertEqual(parse_price("4,800", "JPY"), 4800)

    def test_jpy_fraction_rejected(self):
        with self.assertRaises(ValidationError):
            parse_price("4800.5", "JPY")

    def test_eu_decimal_comma(self):
        self.assertEqual(parse_price("14,90", "EUR", decimal_comma=True), 1490)
        self.assertEqual(parse_price("199,00", "EUR", decimal_comma=True), 19900)
        self.assertEqual(parse_price("1.234,50", "EUR", decimal_comma=True), 123450)

    def test_usd_bare_comma_rejected(self):
        with self.assertRaises(ValidationError):
            parse_price("14,90", "USD")

    def test_negative_rejected(self):
        with self.assertRaises(ValidationError):
            parse_price("-1.00", "USD")

    def test_unknown_currency(self):
        with self.assertRaises(ValidationError):
            parse_price("1.00", "GBP")

    def test_format_minor(self):
        self.assertEqual(format_minor(1999, "USD"), "19.99")
        self.assertEqual(format_minor(4800, "JPY"), "4800")

    def test_extra_usd_fraction_rejected(self):
        with self.assertRaises(ValidationError):
            parse_price("19.999", "USD")


if __name__ == "__main__":
    unittest.main()
