import unittest

import helpers  # noqa: F401
from helpers import make_job, signed_deliveries

from api_reliability_lab.errors import ApiError, AuthError, SchemaError, SimulatedCrash
from api_reliability_lab.seed import build_catalog, build_fault_script, build_webhook_events


class SyncTests(unittest.TestCase):
    def test_happy_path_snapshot_and_webhooks(self):
        job, service, sleeper, logger, clock, transport = make_job()
        events = build_webhook_events()
        report = job.run(deliveries=signed_deliveries(events, clock))
        self.assertFalse(report.dry_run)
        self.assertEqual(report.pages, 3)
        self.assertEqual(report.orders_inserted, 24)
        self.assertEqual(report.orders_rejected, 0)
        self.assertEqual(report.acks_sent, 5)
        self.assertEqual(len(service.ack_state), 5)
        self.assertEqual(report.webhooks_applied, 3)
        self.assertEqual(report.webhooks_duplicate, 2)
        self.assertEqual(report.webhooks_stale, 1)
        self.assertEqual(len(job.ledger.orders), 25)
        self.assertEqual(job.ledger.get("ORD-1025").id, "ORD-1025")
        self.assertEqual(job.store.load("lab-sync").status, "complete")
        self.assertTrue(logger.of_type("sync_complete"))

    def test_fault_script_retries_then_succeeds(self):
        job, service, sleeper, logger, clock, transport = make_job(faults=build_fault_script())
        report = job.run(deliveries=[])
        self.assertEqual(report.orders_inserted, 24)
        # 503, 429, timeout on the first list call; 500 then lost response on the first ack.
        self.assertEqual(report.retries, 5)
        self.assertEqual(len(logger.of_type("request_retry")), 5)
        self.assertEqual(len(service.ack_state), 5)
        # The lost-response ack was committed once and replayed on retry.
        self.assertEqual(report.acks_sent, 4)
        self.assertEqual(report.acks_replayed, 1)
        committed = [item for item in service.ack_attempts if not item["replay"]]
        self.assertEqual(len(committed), 5)

    def test_report_retries_are_counted_per_run(self):
        job, *_ = make_job(
            faults=[{"method": "GET", "path": "/v1/orders", "status": 503}],
            crash_after_pages=1,
            crash_at="post_checkpoint",
        )
        with self.assertRaises(SimulatedCrash):
            job.run(deliveries=[])
        job.crash_after_pages = None
        report = job.run(deliveries=[])
        self.assertEqual(job.client.transport.retry_count, 1)
        self.assertEqual(report.retries, 0)

    def test_health_failure_marks_checkpoint_failed(self):
        job, *_ = make_job(faults=[{"method": "GET", "path": "/health", "status": 400}])
        with self.assertRaises(ApiError):
            job.run(deliveries=[])
        self.assertEqual(job.store.load("lab-sync").status, "failed")

    def test_failed_checkpoint_resumes_from_its_cursor(self):
        catalog = build_catalog()
        job, service, *_ = make_job(catalog=catalog)
        poison = dict(catalog[14])
        poison["id"] = "ORD-1099"
        poison["status"] = "lost"
        poison["updated_at"] = "2026-01-01T14:30:00Z"
        job.fail_fast = True
        service.upsert_order(poison)
        with self.assertRaises(SchemaError):
            job.run(deliveries=[])
        failed = job.store.load("lab-sync")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.pages_done, 1)

        del service.orders["ORD-1099"]
        report = job.run(deliveries=[])
        self.assertTrue(report.resumed)
        self.assertEqual(report.pages, 2)
        self.assertEqual(len(job.ledger.orders), 24)

    def test_dry_run_validates_without_writing(self):
        job, service, *_ = make_job(faults=build_fault_script())
        events = build_webhook_events()
        report = job.run(
            deliveries=signed_deliveries(events, job.clock),
            dry_run=True,
        )
        self.assertTrue(report.dry_run)
        self.assertEqual(report.orders_inserted, 24)
        self.assertEqual(len(job.ledger.orders), 0)
        self.assertEqual(len(service.ack_state), 0)
        self.assertIsNone(job.store.load("lab-sync"))
        self.assertEqual(report.acks_sent, 0)

    def test_dry_run_preview_matches_the_real_run(self):
        events = build_webhook_events()
        job, *_ = make_job()
        preview = job.run(deliveries=signed_deliveries(events, job.clock), dry_run=True)
        self.assertEqual(len(job.ledger.orders), 0)
        real = job.run(deliveries=signed_deliveries(events, job.clock))
        fields = (
            "orders_inserted",
            "orders_ignored_duplicate",
            "webhooks_applied",
            "webhooks_duplicate",
            "webhooks_stale",
        )
        self.assertEqual(
            {name: getattr(preview, name) for name in fields},
            {name: getattr(real, name) for name in fields},
        )
        self.assertEqual((preview.webhooks_applied, preview.webhooks_duplicate), (3, 2))

    def test_poison_item_is_skipped_and_the_rest_of_the_page_commits(self):
        catalog = build_catalog()
        poison = dict(catalog[0])
        poison["id"] = "ORD-1099"
        poison["status"] = "lost"
        poison["updated_at"] = "2026-01-01T00:00:30Z"
        poison["customer_ref"] = "CUST-999"
        poison["items"] = [dict(item) for item in catalog[0]["items"]]
        job, service, *_ = make_job(catalog=catalog + [poison], page_limit=10)
        report = job.run(deliveries=[])
        self.assertEqual(report.orders_rejected, 1)
        self.assertNotIn("ORD-1099", job.ledger.orders)
        self.assertEqual(len(job.ledger.orders), 24)

    def test_fail_fast_stops_before_checkpointing_the_poison_page(self):
        catalog = build_catalog()
        poison = dict(catalog[0])
        poison["id"] = "ORD-1099"
        poison["status"] = "lost"
        poison["updated_at"] = "2026-01-01T00:00:30Z"
        poison["items"] = [dict(item) for item in catalog[0]["items"]]
        job, *_ = make_job(catalog=catalog + [poison], fail_fast=True)
        with self.assertRaises(SchemaError):
            job.run(deliveries=[])
        checkpoint = job.store.load("lab-sync")
        self.assertEqual(checkpoint.status, "failed")
        self.assertEqual(checkpoint.pages_done, 0)

    def test_auth_failure_is_not_retried(self):
        job, service, _, _, _, transport = make_job()
        service.token = "rotated"
        with self.assertRaises(AuthError):
            job.run(deliveries=[])
        self.assertEqual(transport.retry_count, 0)

    def test_completed_checkpoint_is_a_no_op_on_resume(self):
        job, *_ = make_job()
        first = job.run(deliveries=[])
        second = job.run(deliveries=[])
        self.assertEqual(first.pages, 3)
        self.assertEqual(second.pages, 0)
        self.assertTrue(second.resumed)
        self.assertEqual(len(job.ledger.orders), 24)

    def test_acks_use_deterministic_keys_across_retry(self):
        job, service, *_ = make_job(
            faults=[{"method": "POST", "path": "/v1/acks", "status": 500}]
        )
        job.run(deliveries=[])
        keys = [item["idempotency_key"] for item in service.call_log if item["path"] == "/v1/acks"]
        self.assertTrue(keys)
        self.assertTrue(all(key.startswith("lab-sync:ack:") for key in keys if key))
        self.assertEqual(keys.count(keys[0]), 2)


if __name__ == "__main__":
    unittest.main()
