import csv
import tempfile
import unittest
from io import StringIO
from pathlib import Path

import helpers  # noqa: F401

from web_collection_lab.csv_export import csv_path_for, render_csv, write_csv
from web_collection_lab.models import CSV_FIELDS
from web_collection_lab.seed import expected_products


class CsvTests(unittest.TestCase):
    def test_header_order_and_comma_quoting(self):
        text = render_csv(expected_products())
        rows = list(csv.DictReader(StringIO(text)))
        self.assertEqual(list(csv.DictReader(StringIO(text)).fieldnames), list(CSV_FIELDS))
        skus = [row["sku"] for row in rows]
        self.assertEqual(skus, sorted(skus))
        coat = next(row for row in rows if row["sku"] == "SKU-1002")
        self.assertEqual(coat["title"], "Wool Coat, Lined")
        self.assertIn('"Wool Coat, Lined"', text)
        cafe = next(row for row in rows if row["sku"] == "SKU-1003")
        self.assertEqual(cafe["title"], "Café Apron")
        self.assertEqual(cafe["price_cents"], "2450")
        self.assertEqual(cafe["currency"], "EUR")

    def test_atomic_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = csv_path_for(Path(tmp), "lab-collect")
            write_csv(path, expected_products())
            raw = path.read_text(encoding="utf-8")
            self.assertTrue(raw.startswith("sku,title,"))
            self.assertNotIn("\r\n", raw)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
