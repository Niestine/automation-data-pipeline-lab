import unittest

import helpers  # noqa: F401

from maintenance_lab.errors import ParseError
from maintenance_lab.parse import parse_feed, sniff_delimiter
from maintenance_lab.seed import EU_CSV, NEWLINE_CSV, V1_CSV, V2_JSON


class ParseTests(unittest.TestCase):
    def test_v1_csv_quoted_comma_stays_in_title(self):
        _, rows = parse_feed(V1_CSV)
        gamma = next(row for row in rows if row.fields["sku"] == "SKU-1003")
        self.assertEqual(gamma.fields["product_name"], "Widget, Gamma")
        self.assertEqual(gamma.fields["price"], "9.99")

    def test_semicolon_sniff_sets_decimal_comma(self):
        _, rows = parse_feed(EU_CSV)
        self.assertTrue(rows[0].decimal_comma)
        self.assertEqual(rows[0].fields["price"], "14,90")
        self.assertEqual(rows[0].currency_hint, "EUR")

    def test_quoted_newline_is_one_record(self):
        _, rows = parse_feed(NEWLINE_CSV)
        self.assertEqual(len(rows), 1)
        self.assertIn("\n", rows[0].fields["product_name"])

    def test_json_v2_stringifies_bool_and_int(self):
        _, rows = parse_feed(V2_JSON)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].version, "v2")
        self.assertEqual(rows[0].origins["active"], "bool")
        self.assertEqual(rows[0].fields["price_cents"], "2150")
        self.assertEqual(rows[1].fields["price"], "40.00")

    def test_sniff_tab(self):
        self.assertEqual(sniff_delimiter("a\tb\tc"), "\t")

    def test_empty_csv_fails(self):
        with self.assertRaises(ParseError):
            parse_feed("# version=v1 currency=USD\n")

    def test_duplicate_header_fails(self):
        text = "# version=v1 currency=USD\nsku,sku\nA,B\n"
        with self.assertRaises(ParseError):
            parse_feed(text)

    def test_nan_json_is_rejected(self):
        text = '{"version": "v2", "items": [{"sku": "A", "price": NaN, "updated_at": "2026-01-02"}]}'
        with self.assertRaises(ParseError):
            parse_feed(text)

    def test_unknown_column_is_kept_as_extra(self):
        _, rows = parse_feed(V1_CSV)
        self.assertIn("notes", rows[0].extra_columns)

    def test_whitespace_only_body_is_csv_error(self):
        with self.assertRaises(ParseError) as ctx:
            parse_feed("# version=v1 currency=USD\n\n   \n")
        self.assertIn("CSV feed is empty", ctx.exception.message)

    def test_json_extra_root_field_rejected(self):
        text = '{"version": "v2", "items": [], "owner": "ops"}'
        with self.assertRaises(ParseError):
            parse_feed(text)


if __name__ == "__main__":
    unittest.main()
