import unittest

import helpers  # noqa: F401
from helpers import make_pipeline

from maintenance_lab.compat import adapt_row, catalog_diff, normalize_sku, parse_active, parse_stock
from maintenance_lab.errors import ValidationError
from maintenance_lab.parse import parse_feed
from maintenance_lab.seed import EU_CSV, V1_CSV, build_products


class CompatTests(unittest.TestCase):
    def test_v1_field_remaps(self):
        _, rows = parse_feed(V1_CSV)
        record = adapt_row(rows[0])
        self.assertEqual(record.sku, "SKU-1001")
        self.assertEqual(record.title, "Widget Alpha")
        self.assertEqual(record.price_cents, 2150)
        self.assertEqual(record.stock, 12)
        self.assertIn("product_name->title", record.remaps)
        self.assertIn("qty->stock", record.remaps)
        self.assertIn("image->image_url", record.remaps)
        self.assertIn("price->price_cents", record.remaps)

    def test_empty_qty_is_none(self):
        _, rows = parse_feed(V1_CSV)
        beta = next(row for row in rows if row.fields["sku"] == "SKU-1002")
        record = adapt_row(beta)
        self.assertIsNone(record.stock)
        self.assertIn("BUG-005", record.bug_guards)

    def test_string_false(self):
        self.assertEqual(parse_active("false", origin="str"), (False, ("BUG-007",)))
        self.assertEqual(parse_active("false", origin="bool"), (False, ()))
        self.assertEqual(parse_active("yes", origin="str"), (True, ()))

    def test_sku_case_and_length(self):
        sku, remaps, guards = normalize_sku(" sku-1003 ")
        self.assertEqual(sku, "SKU-1003")
        self.assertIn("BUG-006", guards)
        long_sku, _, long_guards = normalize_sku("WIDGET-LONG-NAME-OK")
        self.assertEqual(long_sku, "WIDGET-LONG-NAME-OK")
        self.assertIn("BUG-008", long_guards)
        with self.assertRaises(ValidationError):
            normalize_sku("XX")
        with self.assertRaises(ValidationError):
            normalize_sku("WIDGET-LONG-NAME-THAT-IS-WAY-TOO-LONG-FOR-RULES")

    def test_stock_explicit_zero(self):
        self.assertEqual(parse_stock("0", present=True), (0, ()))

    def test_eu_price_and_remap(self):
        _, rows = parse_feed(EU_CSV)
        record = adapt_row(rows[0])
        self.assertEqual(record.price_cents, 1490)
        self.assertEqual(record.currency, "EUR")
        self.assertIn("BUG-012", record.bug_guards)

    def test_relative_image_rejected(self):
        _, rows = parse_feed(V1_CSV)
        bad = next(row for row in rows if row.fields["sku"] == "SKU-1005")
        with self.assertRaises(ValidationError) as ctx:
            adapt_row(bad)
        self.assertEqual(ctx.exception.field, "image_url")
        self.assertIn("BUG-013", ctx.exception.bug_guards)

    def test_slash_date_rejected(self):
        _, rows = parse_feed(V1_CSV)
        bad = next(row for row in rows if row.fields["sku"] == "SKU-1006")
        with self.assertRaises(ValidationError) as ctx:
            adapt_row(bad)
        self.assertIn("BUG-015", ctx.exception.bug_guards)

    def test_catalog_diff_flags_currency_as_breaking(self):
        pipeline, catalog, *_ = make_pipeline()
        before = catalog.snapshot()
        products = build_products()
        changed = products[0]
        from maintenance_lab.catalog import make_product

        after_map = dict(before)
        after_map["SKU-1001"] = make_product(
            sku=changed.sku,
            title=changed.title,
            price_cents=changed.price_cents,
            currency="EUR",
            stock=changed.stock,
            active=changed.active,
            image_url=changed.image_url,
            version=2,
            updated_at_ms=changed.updated_at_ms,
            source_version=changed.source_version,
        )
        after_map["SKU-9999"] = make_product(
            sku="SKU-9999",
            title="New",
            price_cents=100,
            currency="USD",
            stock=1,
            active=True,
            image_url="https://cdn.example.test/n.png",
            version=1,
            updated_at_ms=changed.updated_at_ms,
            source_version="v2",
        )
        del after_map["SKU-1002"]
        report = catalog_diff(before, after_map)
        self.assertEqual(report.added, ("SKU-9999",))
        self.assertEqual(report.removed, ("SKU-1002",))
        breaking_fields = {(item.sku, item.field) for item in report.breaking}
        self.assertIn(("SKU-1001", "currency"), breaking_fields)
        self.assertIn(("SKU-1002", "sku"), breaking_fields)

    def test_invalid_active_token(self):
        with self.assertRaises(ValidationError):
            parse_active("maybe", origin="str")


if __name__ == "__main__":
    unittest.main()
