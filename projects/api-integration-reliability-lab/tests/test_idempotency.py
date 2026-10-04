import unittest

import helpers  # noqa: F401
from helpers import make_job

from api_reliability_lab.errors import ApiError, IdempotencyConflict, SimulatedCrash, TransportError
from api_reliability_lab.ledger import Ledger
from api_reliability_lab.models import LAB_TOKEN, Order
from api_reliability_lab.seed import build_catalog
from api_reliability_lab.schema import require_order
from api_reliability_lab.transport import HttpRequest


class IdempotencyTests(unittest.TestCase):
    def test_ack_replay_returns_the_stored_body_and_does_not_double_apply(self):
        job, service, *_ = make_job()
        shipped = next(row for row in build_catalog() if row["status"] == "shipped")
        key = f"lab-sync:ack:{shipped['id']}:{shipped['version']}"
        first = job.client.ack_shipment(shipped["id"], shipped["version"], idempotency_key=key)
        second = job.client.ack_shipment(shipped["id"], shipped["version"], idempotency_key=key)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["order_id"], shipped["id"])
        self.assertEqual(len(service.ack_state), 1)
        self.assertEqual(service.ack_state[shipped["id"]], shipped["version"])
        replays = [item for item in service.ack_attempts if item["replay"]]
        self.assertEqual(len(replays), 1)

    def test_lost_response_retry_replays_instead_of_reapplying(self):
        job, service, _, _, _, transport = make_job(
            faults=[{"method": "POST", "path": "/v1/acks", "lose_response": True}]
        )
        shipped = next(row for row in build_catalog() if row["status"] == "shipped")
        key = f"lab-sync:ack:{shipped['id']}:{shipped['version']}"
        result = job.client.ack_shipment(shipped["id"], shipped["version"], idempotency_key=key)
        self.assertTrue(result["replayed"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(transport.retry_count, 1)
        self.assertEqual(
            [item["replay"] for item in service.ack_attempts],
            [False, True],
        )

    def test_lost_response_without_key_is_not_retried(self):
        job, service, _, _, _, transport = make_job(
            faults=[{"method": "POST", "path": "/v1/acks", "lose_response": True}]
        )
        request = HttpRequest(
            "POST",
            "/v1/acks",
            headers={"authorization": f"Bearer {LAB_TOKEN}"},
            body=b'{"order_id":"ORD-1003","version":3,"action":"ack_shipment"}',
        )
        with self.assertRaises(TransportError):
            transport.send(request)
        self.assertEqual(transport.retry_count, 0)

    def test_crash_between_ack_and_checkpoint_replays_acks_on_resume(self):
        job, service, _, _, clock, _ = make_job(crash_after_pages=1, crash_at="post_ack")
        with self.assertRaises(SimulatedCrash):
            job.run(deliveries=[])
        first_page_acks = len(service.ack_state)
        self.assertGreater(first_page_acks, 0)
        self.assertEqual(job.store.load("lab-sync").pages_done, 0)

        # The restart happens later; keys must not depend on time or randomness.
        clock.advance_ms(60_000)
        job.crash_after_pages = None
        report = job.run(deliveries=[])
        keys = {item["key"] for item in service.ack_attempts}
        expected = {
            f"lab-sync:ack:{row['id']}:{row['version']}"
            for row in build_catalog()
            if row["status"] == "shipped"
        }
        self.assertEqual(keys, expected)
        self.assertEqual(report.acks_replayed, first_page_acks)
        self.assertEqual(report.acks_sent, 5 - first_page_acks)
        committed = [item for item in service.ack_attempts if not item["replay"]]
        self.assertEqual(len(committed), 5)
        self.assertEqual(len(service.ack_state), 5)

    def test_same_key_different_body_conflicts(self):
        job, service, *_ = make_job()
        shipped = next(row for row in build_catalog() if row["status"] == "shipped")
        key = "shared-key"
        job.client.ack_shipment(shipped["id"], shipped["version"], idempotency_key=key)
        other = next(
            row
            for row in build_catalog()
            if row["status"] == "shipped" and row["id"] != shipped["id"]
        )
        with self.assertRaises(IdempotencyConflict):
            job.client.ack_shipment(other["id"], other["version"], idempotency_key=key)

    def test_missing_idempotency_key_is_a_bad_request(self):
        job, service, *_ = make_job()
        request = HttpRequest(
            "POST",
            "/v1/acks",
            headers={"authorization": f"Bearer {LAB_TOKEN}", "content-type": "application/json"},
            body=b'{"order_id":"ORD-1003","version":3,"action":"ack_shipment"}',
        )
        response = job.client.transport.send(request)
        self.assertEqual(response.status, 400)

    def test_ack_of_non_shipped_order_is_unprocessable_and_not_retried(self):
        job, _, _, _, _, transport = make_job()
        pending = next(row for row in build_catalog() if row["status"] == "pending")
        with self.assertRaises(ApiError) as ctx:
            job.client.ack_shipment(pending["id"], pending["version"], idempotency_key="k")
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(transport.retry_count, 0)

    def test_ledger_is_versioned_and_idempotent(self):
        ledger = Ledger()
        order = require_order(build_catalog()[0])
        self.assertEqual(ledger.upsert(order, source="snapshot"), "inserted")
        self.assertEqual(ledger.upsert(order, source="snapshot"), "ignored_duplicate")
        newer = Order(
            id=order.id,
            status="paid",
            amount_cents=order.amount_cents,
            currency=order.currency,
            updated_at="2026-01-02T00:00:00Z",
            version=order.version + 1,
            customer_ref=order.customer_ref,
            items=order.items,
        )
        self.assertEqual(ledger.upsert(newer, source="webhook", event_id="evt_0001"), "updated")
        stale = Order(
            id=order.id,
            status="pending",
            amount_cents=order.amount_cents,
            currency=order.currency,
            updated_at=order.updated_at,
            version=order.version,
            customer_ref=order.customer_ref,
            items=order.items,
        )
        self.assertEqual(ledger.upsert(stale, source="webhook", event_id="evt_0009"), "ignored_stale")
        conflict = Order(
            id=newer.id,
            status="cancelled",
            amount_cents=newer.amount_cents,
            currency=newer.currency,
            updated_at=newer.updated_at,
            version=newer.version,
            customer_ref=newer.customer_ref,
            items=newer.items,
        )
        self.assertEqual(
            ledger.upsert(conflict, source="webhook", event_id="evt_0010"),
            "ignored_conflict",
        )
        self.assertEqual(ledger.get(order.id).status, "paid")
        self.assertEqual(
            ledger.upsert(newer, source="webhook", event_id="evt_0001"),
            "ignored_duplicate",
        )

    def test_dry_run_does_not_mutate_ledger(self):
        ledger = Ledger()
        order = require_order(build_catalog()[0])
        self.assertEqual(ledger.upsert(order, source="snapshot", dry_run=True), "inserted")
        self.assertIsNone(ledger.get(order.id))


if __name__ == "__main__":
    unittest.main()
