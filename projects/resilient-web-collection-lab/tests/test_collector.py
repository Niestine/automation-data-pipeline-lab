import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import make_job

from web_collection_lab.catalog import CatalogStore
from web_collection_lab.checkpoint import FileCheckpointStore
from web_collection_lab.errors import SchemaError, SimulatedCrash
from web_collection_lab.seed import expected_products


class CollectorTests(unittest.TestCase):
    def test_default_collect_matches_the_seed_diff(self):
        job, site, sleeper, logger, _clock, transport, _limiter, _inner = make_job(
            faults=[
                {"method": "GET", "path": "/robots.txt", "status": 503},
                {"method": "GET", "path": "/catalog", "status": 429, "headers": {"Retry-After": "0"}},
                {"method": "GET", "path": "/products/sku-1001", "timeout": True},
            ]
        )
        report = job.run()
        self.assertEqual(report.listing_pages, 2)
        self.assertEqual(report.products_parsed, 6)
        self.assertEqual(report.products_rejected, 1)
        self.assertEqual(report.robots_skipped, 1)
        self.assertEqual(report.retries, 3)
        self.assertEqual(report.added, 3)
        self.assertEqual(report.removed, 1)
        self.assertEqual(report.changed, 1)
        self.assertEqual(report.unchanged, 2)
        self.assertEqual(report.csv_rows, 6)
        self.assertEqual(sorted(job.catalog.products), [item.sku for item in expected_products()])
        self.assertEqual(report.changes["added"], ["SKU-1003", "SKU-1004", "SKU-1006"])
        self.assertEqual(report.changes["removed"], ["SKU-OLD1"])
        self.assertEqual(report.changes["changed"]["SKU-1001"][0]["after"], 2900)
        paths = [item["path"] for item in site.call_log]
        self.assertNotIn("/private/hidden", paths)
        self.assertIn("/products/poison", paths)
        self.assertGreaterEqual(transport.retry_count, 3)
        self.assertTrue(any(delay >= 1000 for delay in sleeper.delays))
        cafe = job.catalog.products["SKU-1003"]
        self.assertEqual(cafe.title, "Café Apron")
        self.assertEqual(cafe.price_cents, 2450)
        denied = logger.of_type("robots_denied")
        self.assertEqual(denied[0]["path"], "/private/hidden")

    def test_dry_run_does_not_write_durable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = CatalogStore(Path(tmp) / "lab-collect.snapshot.json")
            store = FileCheckpointStore(tmp)
            job, *_ = make_job(catalog_store=dest, store=store)
            report = job.run(dry_run=True)
            self.assertTrue(report.dry_run)
            self.assertEqual(report.products_parsed, 6)
            self.assertFalse(dest.path.exists())
            self.assertIsNone(store.load("lab-collect"))
            self.assertFalse(list(Path(tmp).glob("*.csv")))

    def test_crash_resume_skips_completed_product_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = CatalogStore(Path(tmp) / "lab-collect.snapshot.json")
            store = FileCheckpointStore(tmp)
            job, site, *_rest = make_job(
                crash_after_products=2,
                catalog_store=dest,
                store=store,
            )
            with self.assertRaises(SimulatedCrash):
                job.run()
            checkpoint = store.load("lab-collect")
            self.assertEqual(checkpoint.status, "in_progress")
            self.assertEqual(len(checkpoint.completed_paths), 2)
            self.assertEqual(len(dest.products), 2)

            job2, site2, *_rest2 = make_job(catalog_store=dest, store=store)
            report = job2.run(resume=True)
            self.assertTrue(report.resumed)
            self.assertEqual(report.products_parsed, 4)
            self.assertEqual(len(dest.products), 6)
            self.assertEqual(store.load("lab-collect").status, "complete")
            first_paths = [item["path"] for item in site.call_log if item["path"].startswith("/products/")]
            second_paths = [item["path"] for item in site2.call_log if item["path"].startswith("/products/")]
            self.assertTrue(first_paths)
            for path in checkpoint.completed_paths:
                self.assertNotIn(path, second_paths)

    def test_second_run_uses_etags_for_304(self):
        job, site, *_ = make_job(previous={"origin": "https://fixture.example.invalid", "collected_at": "2026-01-04T12:00:00Z", "job_id": "x", "products": [], "etags": {}})
        first = job.run()
        self.assertEqual(first.products_not_modified, 0)
        snapshot = job.catalog.to_dict()
        job2, site2, *_rest = make_job(previous=snapshot)
        second = job2.run()
        self.assertEqual(second.products_not_modified, 6)
        self.assertEqual(second.unchanged, 6)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.changed, 0)
        self.assertEqual(second.removed, 0)
        statuses_304 = sum(
            1
            for item in site2.call_log
            if item["path"].startswith("/products/") and item["if_none_match"]
        )
        self.assertGreaterEqual(statuses_304, 6)

    def test_fail_fast_stops_on_poison_row(self):
        job, *_ = make_job(fail_fast=True)
        with self.assertRaises(SchemaError):
            job.run()

    def test_force_ignores_etags(self):
        job, *_ = make_job(previous={"origin": "https://fixture.example.invalid", "collected_at": "2026-01-04T12:00:00Z", "job_id": "x", "products": [], "etags": {}})
        job.run()
        snapshot = job.catalog.to_dict()
        job2, site2, *_rest = make_job(previous=snapshot, force=True)
        report = job2.run()
        self.assertEqual(report.products_not_modified, 0)
        self.assertEqual(report.products_parsed, 6)
        self.assertTrue(all(not item["if_none_match"] for item in site2.call_log if item["path"].startswith("/products/")))

    def test_robots_404_allows_all_but_still_collects(self):
        job, site, *_ = make_job(
            robots_txt="",
            faults=[{"method": "GET", "path": "/robots.txt", "status": 404}],
            previous={"origin": "https://fixture.example.invalid", "collected_at": "2026-01-04T12:00:00Z", "job_id": "x", "products": [], "etags": {}},
        )
        report = job.run()
        # Empty robots after 404: private page is allowed and collected.
        self.assertEqual(report.robots_skipped, 0)
        self.assertIn("SKU-9999", job.catalog.products)

    def test_crawl_delay_spaces_every_request_including_retries(self):
        job, site, _sleeper, logger, *_ = make_job(
            faults=[
                {"method": "GET", "path": "/catalog", "status": 429, "headers": {"Retry-After": "0"}},
                {"method": "GET", "path": "/products/sku-1001", "timeout": True},
            ]
        )
        job.run()
        starts = [item["ts_ms"] for item in logger.of_type("request_start")]
        self.assertEqual(len(starts), len(site.call_log))
        # robots.txt is fetched before Crawl-delay is known; every later
        # request, retries included, is at least 1000 ms after the previous one.
        gaps = [later - earlier for earlier, later in zip(starts[1:], starts[2:])]
        self.assertTrue(gaps)
        self.assertGreaterEqual(min(gaps), 1000)

    def test_robots_disallowed_listing_is_never_fetched(self):
        robots = "User-agent: LabCollector\nDisallow: /catalog\n"
        job, site, *_ = make_job(robots_txt=robots)
        report = job.run()
        paths = [item["path"] for item in site.call_log]
        self.assertEqual(paths, ["/robots.txt"])
        self.assertEqual(report.listing_pages, 0)
        self.assertEqual(report.robots_skipped, 1)
        self.assertEqual(report.csv_rows, 0)

    def test_checkpoint_paths_follow_fetch_order(self):
        job, site, *_ = make_job()
        report = job.run()
        fetched = [
            item["path"]
            for item in site.call_log
            if item["path"].startswith("/products/")
        ]
        self.assertEqual(report.checkpoint["completed_paths"], fetched)

    def test_fresh_run_drops_rows_left_in_the_destination(self):
        stale = expected_products()[0]
        dest = CatalogStore()
        dest.upsert(stale.__class__(**{**stale.to_dict(), "sku": "SKU-GONE1"}))
        job, *_ = make_job(catalog_store=dest)
        report = job.run()
        self.assertNotIn("SKU-GONE1", dest.products)
        self.assertEqual(report.csv_rows, 6)

    def test_resume_keeps_the_original_collected_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = CatalogStore(Path(tmp) / "lab-collect.snapshot.json")
            store = FileCheckpointStore(tmp)
            job, *_ = make_job(crash_after_products=2, catalog_store=dest, store=store)
            with self.assertRaises(SimulatedCrash):
                job.run()
            first_stamp = dest.collected_at

            resumed_dest = CatalogStore(dest.path)
            job2, _site2, _sleeper2, _logger2, clock2, *_ = make_job(catalog_store=resumed_dest, store=store)
            clock2.advance_ms(3_600_000)
            report = job2.run()
            self.assertTrue(report.resumed)
            self.assertEqual(report.collected_at, first_stamp)
            self.assertEqual({item.collected_at for item in resumed_dest.products.values()}, {first_stamp})
            self.assertEqual(len(resumed_dest.products), 6)


if __name__ == "__main__":
    unittest.main()
