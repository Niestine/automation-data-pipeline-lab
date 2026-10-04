import unittest

import helpers  # noqa: F401

from web_collection_lab.errors import SchemaError
from web_collection_lab.schema import validate_product_dict, validate_snapshot
from web_collection_lab.seed import build_previous_snapshot, expected_products


class SchemaTests(unittest.TestCase):
    def test_canonical_products_validate(self):
        for product in expected_products():
            again = validate_product_dict(product.to_dict())
            self.assertEqual(again.content_hash, product.content_hash)

    def test_previous_snapshot_validates(self):
        parsed = validate_snapshot(build_previous_snapshot())
        self.assertEqual(len(parsed["products"]), 4)

    def test_wrong_hash_is_rejected(self):
        row = expected_products()[0].to_dict()
        row["content_hash"] = "0" * 64
        with self.assertRaises(SchemaError):
            validate_product_dict(row)

    def test_extra_fields_are_rejected(self):
        row = expected_products()[0].to_dict()
        row["discount"] = 5
        with self.assertRaises(SchemaError):
            validate_product_dict(row)

    def test_bool_is_not_an_integer_price(self):
        row = expected_products()[0].to_dict()
        row["price_cents"] = True
        with self.assertRaises(SchemaError):
            validate_product_dict(row)

    def test_duplicate_sku_in_snapshot_is_rejected(self):
        product = expected_products()[0].to_dict()
        payload = {
            "origin": "https://fixture.example.invalid",
            "collected_at": "2026-01-04T12:00:00Z",
            "job_id": "x",
            "products": [product, dict(product)],
            "etags": {},
        }
        with self.assertRaises(SchemaError):
            validate_snapshot(payload)


if __name__ == "__main__":
    unittest.main()
