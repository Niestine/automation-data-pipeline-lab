import unittest

import helpers  # noqa: F401

from web_collection_lab.detect import diff_products
from web_collection_lab.schema import validate_snapshot
from web_collection_lab.seed import build_previous_snapshot, expected_products


class DetectTests(unittest.TestCase):
    def test_weekly_diff_against_the_seed_snapshot(self):
        previous = validate_snapshot(build_previous_snapshot())["products"]
        current = expected_products()
        changes = diff_products(previous, current)
        self.assertEqual(changes.added, ("SKU-1003", "SKU-1004", "SKU-1006"))
        self.assertEqual(changes.removed, ("SKU-OLD1",))
        self.assertEqual(changes.unchanged, ("SKU-1002", "SKU-1005"))
        self.assertEqual(len(changes.changed), 1)
        sku, fields = changes.changed[0]
        self.assertEqual(sku, "SKU-1001")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].field, "price_cents")
        self.assertEqual(fields[0].before, 2800)
        self.assertEqual(fields[0].after, 2900)

    def test_identical_catalogs_are_unchanged(self):
        products = expected_products()
        changes = diff_products(products, products)
        self.assertEqual(changes.added, ())
        self.assertEqual(changes.removed, ())
        self.assertEqual(changes.changed, ())
        self.assertEqual(len(changes.unchanged), 6)

    def test_collected_at_does_not_count_as_a_change(self):
        left = expected_products(collected_at="2026-01-04T12:00:00Z")
        right = expected_products(collected_at="2026-01-11T12:00:00Z")
        changes = diff_products(left, right)
        self.assertEqual(len(changes.unchanged), 6)
        self.assertEqual(changes.changed, ())


if __name__ == "__main__":
    unittest.main()
