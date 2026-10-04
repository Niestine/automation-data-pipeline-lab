import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import feed, make_pipeline

from maintenance_lab.apply import MaintenancePipeline
from maintenance_lab.catalog import CatalogStore
from maintenance_lab.checkpoint import FileCheckpointStore
from maintenance_lab.errors import SimulatedCrash, ValidationError
from maintenance_lab.ledger import FeedLedger
from maintenance_lab.models import LAB_NOW_MS
from maintenance_lab.seed import V1_CSV, V2_JSON, build_default_feeds, build_products
from maintenance_lab.telemetry import JsonLogger, ManualClock


class ApplyTests(unittest.TestCase):
    def test_default_weekly_ingest_counts(self):
        pipeline, catalog, logger, *_ = make_pipeline()
        report = pipeline.ingest(build_default_feeds())
        counts = report.counts()
        self.assertEqual(report.week_id, "2026-W01")
        v2 = next(item for item in report.feeds if item.name.endswith(".json"))
        self.assertEqual(v2.version, "v2")
        self.assertEqual(counts["inserted"], 8)
        self.assertEqual(counts["updated"], 3)
        self.assertEqual(counts["replayed"], 2)
        self.assertEqual(counts["rejected"], 3)
        self.assertEqual(counts["conflict"], 1)
        self.assertEqual(report.catalog_size, 12)
        self.assertEqual(catalog.get("SKU-1001").price_cents, 2150)
        self.assertEqual(catalog.get("SKU-1001").stock, 12)
        self.assertEqual(catalog.get("SKU-1002").stock, 5)
        self.assertFalse(catalog.get("SKU-1004").active)
        self.assertEqual(catalog.get("JP-2001").price_cents, 4800)
        self.assertEqual(catalog.get("EU-3001").price_cents, 1490)
        self.assertEqual(catalog.get("SKU-2001").price_cents, 4000)
        self.assertEqual(catalog.get("SKU-9001").title, "Line one Line two")
        self.assertEqual(catalog.get("EU-4001").title, "Café Linen")
        self.assertTrue(logger.of_type("run_complete"))

    def test_feed_reimport_is_ledger_replay(self):
        pipeline, catalog, *_ = make_pipeline()
        feeds = build_default_feeds()
        first = pipeline.ingest(feeds)
        stock = catalog.get("SKU-1001").stock
        size = len(catalog)
        second = pipeline.ingest(feeds)
        self.assertEqual(second.counts()["feed_replays"], len(feeds))
        self.assertEqual(catalog.get("SKU-1001").stock, stock)
        self.assertEqual(len(catalog), size)
        self.assertEqual(first.counts()["feed_replays"], 0)
        self.assertEqual(second.counts()["rows"], 0)

    def test_stale_row_skipped(self):
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("v1.csv", V1_CSV)])
        old = """# version=v1 currency=USD
sku,product_name,price,qty,active,image,updated_at
SKU-1001,Widget Alpha,1.00,99,true,https://cdn.example.test/sku-1001.png,2025-12-01
"""
        report = pipeline.ingest([feed("old.csv", old)])
        outcome = report.feeds[0].outcomes[0]
        self.assertEqual(outcome.status, "stale")
        self.assertEqual(catalog.get("SKU-1001").price_cents, 2150)
        self.assertNotEqual(catalog.get("SKU-1001").stock, 99)

    def test_future_timestamp_rejected(self):
        text = """# version=v1 currency=USD
sku,product_name,price,qty,active,image,updated_at
SKU-5555,Future,1.00,1,true,https://cdn.example.test/x.png,2026-12-01
"""
        pipeline, *_ = make_pipeline()
        report = pipeline.ingest([feed("future.csv", text)])
        self.assertEqual(report.feeds[0].outcomes[0].status, "rejected")
        self.assertIn("future", report.feeds[0].outcomes[0].message)

    def test_fail_fast_raises(self):
        pipeline, *_ = make_pipeline(fail_fast=True)
        with self.assertRaises(ValidationError):
            pipeline.ingest([feed("v1.csv", V1_CSV)])

    def test_crash_and_resume_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = CatalogStore(root / "catalog.json", products=build_products())
            ledger = FeedLedger(root / "ledger.json")
            checkpoints = FileCheckpointStore(root / "checkpoints")
            clock = ManualClock(LAB_NOW_MS)
            crashing = MaintenancePipeline(
                catalog,
                ledger=ledger,
                checkpoints=checkpoints,
                logger=JsonLogger(clock=clock),
                clock=clock,
                crash_after_rows=1,
            )
            with self.assertRaises(SimulatedCrash):
                crashing.ingest([feed("v1.csv", V1_CSV)])
            self.assertEqual(len([p for p in catalog.products() if p.version > 1 or p.sku == "SKU-1003"]), 1)
            resumed = MaintenancePipeline(
                catalog,
                ledger=ledger,
                checkpoints=checkpoints,
                logger=JsonLogger(clock=ManualClock(LAB_NOW_MS)),
                clock=ManualClock(LAB_NOW_MS),
            )
            report = resumed.ingest([feed("v1.csv", V1_CSV)])
            gamma = [item for item in report.all_outcomes() if item.sku == "SKU-1003" and item.status == "inserted"]
            self.assertEqual(len(gamma), 1)
            self.assertEqual(catalog.get("SKU-1001").price_cents, 2150)

    def test_v2_replays_matching_v1_snapshot(self):
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("v1.csv", V1_CSV)])
        version = catalog.get("SKU-1001").version
        report = pipeline.ingest([feed("v2.json", V2_JSON)])
        replayed = [item for item in report.all_outcomes() if item.sku == "SKU-1001"]
        self.assertEqual(replayed[0].status, "replayed")
        self.assertEqual(catalog.get("SKU-1001").version, version)
        self.assertEqual(catalog.get("SKU-2001").title, "Nexus Hub")

    def test_unquoted_delimiter_row_rejected_not_shifted(self):
        text = """# version=v1 currency=USD
sku,product_name,price,qty,active,image,updated_at
SKU-4001,Widget, Unquoted,9.99,4,true,https://cdn.example.test/a.png,2026-01-02
SKU-4002,Trailing Empty,9.99,4,true,https://cdn.example.test/b.png,2026-01-02,,
"""
        pipeline, catalog, logger, *_ = make_pipeline()
        report = pipeline.ingest([feed("shifted.csv", text)])
        shifted, trailing = report.feeds[0].outcomes
        self.assertEqual(shifted.status, "rejected")
        self.assertIn("BUG-002", shifted.bug_guards)
        self.assertIsNone(catalog.get("SKU-4001"))
        self.assertEqual(trailing.status, "inserted")
        event = logger.of_type("row_rejected")[0]
        self.assertEqual(event["line"], 3)
        self.assertEqual(event["sku"], "SKU-4001")

    def test_force_reapplies_after_completed_checkpoint(self):
        pipeline, catalog, *_ = make_pipeline()
        pipeline.ingest([feed("v1.csv", V1_CSV)])
        forced, *_rest = make_pipeline(
            catalog=catalog,
            ledger=pipeline.ledger,
            checkpoints=pipeline.checkpoints,
            force=True,
        )
        report = forced.ingest([feed("v1.csv", V1_CSV)])
        counts = report.counts()
        self.assertEqual(counts["feed_replays"], 0)
        self.assertEqual(counts["rows"], 9)
        self.assertEqual(counts["inserted"], 0)
        self.assertEqual(counts["replayed"], 5)
        self.assertEqual(counts["conflict"], 1)
        self.assertEqual(counts["rejected"], 3)

    def test_rejected_row_bounds_match_catalog_schema(self):
        long_image = "https://cdn.example.test/" + "a" * 500
        text = f"""# version=v1 currency=USD
sku,product_name,price_cents,qty,active,image,updated_at
SKU-4101,Too Expensive,99999999999,1,true,https://cdn.example.test/a.png,2026-01-02
SKU-4102,Long Image,100,1,true,{long_image},2026-01-02
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            store = CatalogStore(path, products=build_products())
            pipeline, *_rest = make_pipeline(catalog=store)
            report = pipeline.ingest([feed("bounds.csv", text)])
            self.assertEqual([item.status for item in report.all_outcomes()], ["rejected", "rejected"])
            self.assertEqual(len(CatalogStore(path)), 4)

    def test_structured_events_include_ts(self):
        pipeline, _, logger, *_ = make_pipeline()
        pipeline.ingest([feed("v2.json", V2_JSON)])
        start = logger.of_type("run_start")[0]
        self.assertEqual(start["ts_ms"], LAB_NOW_MS)
        self.assertIn("week_id", start)


if __name__ == "__main__":
    unittest.main()
