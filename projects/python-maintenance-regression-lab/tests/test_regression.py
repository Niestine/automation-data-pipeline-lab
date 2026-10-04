import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import feed, make_pipeline

from maintenance_lab.bugs import BUGS, bug_by_id, bug_ids
from datetime import datetime, timedelta, timezone

from maintenance_lab.catalog import CatalogStore
from maintenance_lab.decode import decode_feed
from maintenance_lab.errors import ValidationError
from maintenance_lab.models import FeedInput
from maintenance_lab.money import parse_price
from maintenance_lab.parse import parse_feed
from maintenance_lab.seed import (
    EU_CSV,
    LATIN1_CSV,
    NEWLINE_CSV,
    V1_CSV,
    build_default_feeds,
    build_products,
    jp_cp932_bytes,
    latin1_bytes,
)
from maintenance_lab.window import iso_week_bounds, iso_week_id, ms_from_utc, parse_iso_datetime


class HistoricalBugTests(unittest.TestCase):
    def test_every_bug_has_a_regression_method(self):
        self.assertEqual(len(BUGS), 16)
        self.assertEqual(len(set(bug_ids())), 16)
        for bug in BUGS:
            self.assertTrue(hasattr(self, bug.regression_test), bug.id)
            self.assertEqual(bug_by_id(bug.id).title, bug.title)

    def test_bug_001_shift_jis_not_utf8(self):
        raw = jp_cp932_bytes()
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")
        pipeline, catalog, *_ = make_pipeline()
        report = pipeline.ingest([FeedInput(name="jp.cp932", data=raw)])
        self.assertEqual(catalog.get("JP-2001").title, "木綿シャツ")
        self.assertIn("BUG-001", report.bug_guards())

    def test_bug_002_quoted_comma_in_title(self):
        naive = V1_CSV.splitlines()
        gamma_line = next(line for line in naive if line.startswith("SKU-1003,"))
        self.assertGreater(gamma_line.count(","), 7)
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("v1.csv", V1_CSV)])
        self.assertEqual(catalog.get("SKU-1003").title, "Widget, Gamma")
        self.assertEqual(catalog.get("SKU-1003").price_cents, 999)

    def test_bug_003_float_price_rounding(self):
        self.assertEqual(int(float("19.99") * 100), 1998)
        self.assertEqual(parse_price("19.99", "USD"), 1999)
        self.assertEqual(parse_price("1.13", "USD"), 113)
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("v1.csv", V1_CSV)])
        self.assertEqual(catalog.get("SKU-1001").price_cents, 2150)

    def test_bug_004_utc_iso_week_not_local(self):
        sunday_utc = ms_from_utc(datetime(2026, 1, 4, 16, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(iso_week_id(sunday_utc), "2026-W01")
        naive_jst = datetime(2026, 1, 4, 16, 0, 0, tzinfo=timezone.utc) + timedelta(hours=9)
        self.assertEqual(naive_jst.date().isoformat(), "2026-01-05")
        start, end = iso_week_bounds("2026-W01")
        self.assertLess(start, sunday_utc)
        self.assertEqual(iso_week_id(end), "2026-W02")
        self.assertEqual(iso_week_id(end - 1), "2026-W01")

    def test_bug_005_empty_qty_does_not_zero_stock(self):
        pipeline, catalog, *_ = make_pipeline()
        self.assertEqual(catalog.get("SKU-1002").stock, 5)
        report = pipeline.ingest([feed("v1.csv", V1_CSV)])
        self.assertEqual(catalog.get("SKU-1002").stock, 5)
        beta = next(item for item in report.all_outcomes() if item.sku == "SKU-1002")
        self.assertIn("BUG-005", beta.bug_guards)

    def test_bug_006_sku_case_fold(self):
        pipeline, catalog, *_ = make_pipeline()
        report = pipeline.ingest([feed("v1.csv", V1_CSV)])
        self.assertIsNotNone(catalog.get("SKU-1003"))
        self.assertIsNone(catalog.get("sku-1003"))
        conflict = next(item for item in report.all_outcomes() if item.status == "conflict")
        self.assertEqual(conflict.sku, "SKU-1003")
        self.assertIn("BUG-006", conflict.bug_guards)

    def test_bug_007_string_false_is_inactive(self):
        self.assertTrue(bool("false"))
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("v1.csv", V1_CSV)])
        self.assertFalse(catalog.get("SKU-1004").active)
        self.assertFalse(catalog.get("SKU-1007").active)

    def test_bug_008_sku_not_truncated(self):
        text = """# version=v1 currency=USD
sku,product_name,price,qty,active,image,updated_at
WIDGET-LONG-NAME-A,Alpha,1.00,1,true,https://cdn.example.test/a.png,2026-01-02
WIDGET-LONG-NAME-B,Beta,1.00,1,true,https://cdn.example.test/b.png,2026-01-02
"""
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("long.csv", text)])
        self.assertIsNotNone(catalog.get("WIDGET-LONG-NAME-A"))
        self.assertIsNotNone(catalog.get("WIDGET-LONG-NAME-B"))
        self.assertIsNone(catalog.get("WIDGET-LONG"))

    def test_bug_009_stale_snapshot_not_applied(self):
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("v1.csv", V1_CSV)])
        old = """# version=v1 currency=USD
sku,product_name,price,qty,active,image,updated_at
SKU-1001,Widget Alpha,1.00,1,true,https://cdn.example.test/sku-1001.png,2025-01-01
"""
        report = pipeline.ingest([feed("old.csv", old)])
        self.assertEqual(report.feeds[0].outcomes[0].status, "stale")
        self.assertIn("BUG-009", report.feeds[0].outcomes[0].bug_guards)
        self.assertEqual(catalog.get("SKU-1001").price_cents, 2150)

    def test_bug_010_dry_run_does_not_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            store = CatalogStore(path, products=build_products())
            pipeline, *_rest = make_pipeline(catalog=store)
            before = path.read_text(encoding="utf-8")
            report = pipeline.ingest(build_default_feeds(), dry_run=True)
            self.assertGreater(report.counts()["inserted"], 0)
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual(len(store), 4)
            self.assertEqual(store.get("SKU-1001").price_cents, 1999)

    def test_bug_011_quoted_newline_in_title(self):
        _, rows = parse_feed(NEWLINE_CSV)
        self.assertEqual(len(rows), 1)
        pipeline, catalog, *_ = make_pipeline()
        report = pipeline.ingest([feed("nl.csv", NEWLINE_CSV)])
        self.assertEqual(catalog.get("SKU-9001").title, "Line one Line two")
        outcome = report.feeds[0].outcomes[0]
        self.assertIn("BUG-011", outcome.bug_guards)

    def test_bug_012_eu_decimal_comma(self):
        self.assertEqual(parse_price("14,90", "EUR", decimal_comma=True), 1490)
        pipeline, catalog, *_ = make_pipeline()
        report = pipeline.ingest([feed("eu.csv", EU_CSV)])
        self.assertEqual(catalog.get("EU-3001").price_cents, 1490)
        self.assertEqual(catalog.get("EU-3002").price_cents, 19900)
        self.assertIn("BUG-012", report.feeds[0].outcomes[0].bug_guards)

    def test_bug_013_relative_image_rejected(self):
        pipeline, catalog, *_ = make_pipeline()
        report = pipeline.ingest([feed("v1.csv", V1_CSV)])
        self.assertIsNone(catalog.get("SKU-1005"))
        rejected = next(item for item in report.all_outcomes() if item.sku == "SKU-1005")
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("BUG-013", rejected.bug_guards)

    def test_bug_014_reimport_does_not_double_stock(self):
        pipeline, catalog, *_ = make_pipeline()
        feeds = [feed("v1.csv", V1_CSV)]
        pipeline.ingest(feeds)
        stock = catalog.get("SKU-1001").stock
        second = pipeline.ingest(feeds)
        self.assertEqual(second.counts()["feed_replays"], 1)
        self.assertEqual(catalog.get("SKU-1001").stock, stock)
        self.assertEqual(stock, 12)

    def test_bug_015_slash_date_rejected(self):
        with self.assertRaises(ValidationError):
            parse_iso_datetime("07/01/2026")
        pipeline, catalog, *_ = make_pipeline()
        report = pipeline.ingest([feed("v1.csv", V1_CSV)])
        rejected = next(item for item in report.all_outcomes() if item.sku == "SKU-1006")
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("BUG-015", rejected.bug_guards)
        self.assertIsNone(catalog.get("SKU-1006"))

    def test_bug_016_latin1_not_cp932(self):
        raw = latin1_bytes()
        decoded = decode_feed(raw)
        self.assertEqual(decoded.encoding, "latin-1")
        self.assertIn("Café", decoded.text)
        pipeline, catalog, *_ = make_pipeline()
        report = pipeline.ingest([feed("latin1.csv", LATIN1_CSV, encoding="latin-1")])
        self.assertEqual(catalog.get("EU-4001").title, "Café Linen")
        self.assertIn("BUG-016", report.feeds[0].outcomes[0].bug_guards)


if __name__ == "__main__":
    unittest.main()
