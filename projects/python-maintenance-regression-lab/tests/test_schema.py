import unittest

import helpers  # noqa: F401

from maintenance_lab.errors import SchemaError
from maintenance_lab.schema import check_schema, load_catalog_payload, parse_json_text
from maintenance_lab.seed import catalog_payload


class SchemaTests(unittest.TestCase):
    def test_default_catalog_loads(self):
        rows = load_catalog_payload(catalog_payload())
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["sku"], "SKU-1001")
        self.assertEqual(len(rows[0]["fingerprint"]), 64)

    def test_extra_catalog_field_rejected(self):
        data = catalog_payload()
        data["owner"] = "ops"
        with self.assertRaises(SchemaError) as ctx:
            load_catalog_payload(data)
        self.assertIn("additional property", ctx.exception.message)

    def test_duplicate_sku_rejected(self):
        data = catalog_payload()
        data["products"].append(dict(data["products"][0]))
        with self.assertRaises(SchemaError) as ctx:
            load_catalog_payload(data)
        self.assertIn("duplicate sku", ctx.exception.message)

    def test_bool_is_not_an_integer(self):
        errors = check_schema(True, {"type": "integer"}, "$")
        self.assertTrue(errors)

    def test_nan_json_is_rejected(self):
        with self.assertRaises(SchemaError):
            parse_json_text('{"price": NaN}')

    def test_infinity_json_is_rejected(self):
        with self.assertRaises(SchemaError):
            parse_json_text('{"price": Infinity}')

    def test_fingerprint_mismatch_rejected(self):
        data = catalog_payload()
        data["products"][0]["fingerprint"] = "0" * 64
        with self.assertRaises(SchemaError):
            load_catalog_payload(data)

    def test_catalog_must_be_object(self):
        with self.assertRaises(SchemaError):
            load_catalog_payload([])

    def test_invalid_sku_pattern(self):
        data = catalog_payload()
        data["products"][0]["sku"] = "xx"
        with self.assertRaises(SchemaError):
            load_catalog_payload(data)


if __name__ == "__main__":
    unittest.main()
