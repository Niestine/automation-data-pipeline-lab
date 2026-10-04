import unittest

import helpers  # noqa: F401

from web_collection_lab.decode import decode_html
from web_collection_lab.errors import SchemaError
from web_collection_lab.fixture_site import render_poison, render_product
from web_collection_lab.models import FIXTURE_ORIGIN
from web_collection_lab.normalize import (
    collapse_ws,
    detect_currency,
    normalize_product,
    parse_price,
    same_origin_path,
)
from web_collection_lab.parse import parse_product
from web_collection_lab.seed import COLLECTED_AT, build_catalog, canonical_from_row


class NormalizeTests(unittest.TestCase):
    def test_collapse_ws_treats_nbsp_as_space(self):
        self.assertEqual(collapse_ws("Breathable\xa0&\xa0durable."), "Breathable & durable.")

    def test_usd_eur_jpy_prices(self):
        self.assertEqual(parse_price("$29.00", "USD"), 2900)
        self.assertEqual(parse_price("$1,299.00", "USD"), 129900)
        self.assertEqual(parse_price("24,50 €", "EUR"), 2450)
        self.assertEqual(parse_price("1.990,00 EUR", "EUR"), 199000)
        self.assertEqual(parse_price("¥2,400", "JPY"), 2400)

    def test_float_rounding_trap_is_avoided(self):
        self.assertEqual(parse_price("19.99", "USD"), 1999)

    def test_jpy_rejects_fractional_yen(self):
        with self.assertRaises(SchemaError):
            parse_price("2400.5", "JPY")

    def test_non_plain_amounts_are_schema_errors(self):
        for raw in ("NaN", "Infinity", "1e3", "$1E2", "-5.00", "$ 12.3.4", "abc"):
            with self.subTest(raw=raw):
                with self.assertRaises(SchemaError):
                    parse_price(raw, "USD")

    def test_currency_detection_from_symbols(self):
        self.assertEqual(detect_currency("$29.00"), "USD")
        self.assertEqual(detect_currency("24,50 €"), "EUR")
        self.assertEqual(detect_currency("¥2,400"), "JPY")
        self.assertEqual(detect_currency("29.00", "USD"), "USD")

    def test_catalog_rows_normalize_to_canonical_products(self):
        for row in build_catalog():
            body, ctype = render_product(row)
            html, _enc = decode_html(body, ctype)
            parsed = parse_product(html)
            product = normalize_product(
                parsed,
                source_url=f"{FIXTURE_ORIGIN}/products/{row['slug']}",
                collected_at=COLLECTED_AT,
            )
            expected = canonical_from_row(row)
            self.assertEqual(product.sku, expected.sku)
            self.assertEqual(product.title, expected.title)
            self.assertEqual(product.price_cents, expected.price_cents)
            self.assertEqual(product.currency, expected.currency)
            self.assertEqual(product.availability, expected.availability)
            self.assertEqual(product.color, expected.color)
            self.assertEqual(product.size, expected.size)
            self.assertEqual(product.image_url, expected.image_url)
            self.assertEqual(product.description, expected.description)
            self.assertEqual(product.content_hash, expected.content_hash)

    def test_data_cents_mismatch_is_rejected(self):
        body, ctype = render_poison()
        html, _enc = decode_html(body, ctype)
        parsed = parse_product(html)
        with self.assertRaises(SchemaError) as ctx:
            normalize_product(
                parsed,
                source_url=f"{FIXTURE_ORIGIN}/products/poison",
                collected_at=COLLECTED_AT,
            )
        self.assertEqual(ctx.exception.field, "price")

    def test_same_origin_resolution_and_off_site_rejection(self):
        base = f"{FIXTURE_ORIGIN}/catalog"
        self.assertEqual(
            same_origin_path("/products/sku-1001", base=base),
            ("/products/sku-1001", {}),
        )
        self.assertEqual(
            same_origin_path("/catalog?page=2", base=base),
            ("/catalog", {"page": "2"}),
        )
        self.assertIsNone(same_origin_path("https://other.example/products/x", base=base))
        self.assertIsNone(same_origin_path("javascript:alert(1)", base=base))


if __name__ == "__main__":
    unittest.main()
