import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from maintenance_lab.catalog import CatalogStore, make_product
from maintenance_lab.errors import StateError
from maintenance_lab.seed import build_products


class CatalogTests(unittest.TestCase):
    def test_memory_roundtrip(self):
        store = CatalogStore(products=build_products())
        self.assertEqual(len(store), 4)
        self.assertEqual(store.get("SKU-1001").stock, 10)

    def test_file_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            store = CatalogStore(path, products=build_products())
            extra = make_product(
                sku="SKU-7777",
                title="Persisted",
                price_cents=100,
                currency="USD",
                stock=1,
                active=True,
                image_url="https://cdn.example.test/p.png",
                version=1,
                updated_at_ms=1,
                source_version="v2",
            )
            store.put(extra)
            reloaded = CatalogStore(path)
            self.assertEqual(len(reloaded), 5)
            self.assertEqual(reloaded.get("SKU-7777").title, "Persisted")

    def test_dry_run_restores(self):
        store = CatalogStore(products=build_products())
        store.begin_dry_run()
        extra = make_product(
            sku="SKU-7777",
            title="Ghost",
            price_cents=100,
            currency="USD",
            stock=1,
            active=True,
            image_url=None,
            version=1,
            updated_at_ms=1,
            source_version="v2",
        )
        store.put(extra)
        self.assertEqual(len(store), 5)
        store.abort_dry_run()
        self.assertEqual(len(store), 4)
        self.assertIsNone(store.get("SKU-7777"))

    def test_dry_run_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            store = CatalogStore(path, products=build_products())
            original = path.read_text(encoding="utf-8")
            store.begin_dry_run()
            extra = make_product(
                sku="SKU-7777",
                title="Ghost",
                price_cents=100,
                currency="USD",
                stock=1,
                active=True,
                image_url=None,
                version=1,
                updated_at_ms=1,
                source_version="v2",
            )
            store.put(extra)
            store.abort_dry_run()
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_corrupt_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(StateError):
                CatalogStore(path)

    def test_snapshot_is_a_copy(self):
        store = CatalogStore(products=build_products())
        snap = store.snapshot()
        snap.pop("SKU-1001")
        self.assertIsNotNone(store.get("SKU-1001"))


if __name__ == "__main__":
    unittest.main()
