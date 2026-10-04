import unittest

import helpers  # noqa: F401
from helpers import make_job

from api_reliability_lab.errors import ApiError, SchemaError
from api_reliability_lab.models import LAB_TOKEN
from api_reliability_lab.mock_service import MockFulfillmentApi, encode_cursor
from api_reliability_lab.seed import build_catalog
from api_reliability_lab.client import build_client
from api_reliability_lab.retry import RetryPolicy
from api_reliability_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper


class PaginationTests(unittest.TestCase):
    def test_keyset_walk_returns_every_order_once(self):
        job, service, *_ = make_job(page_limit=10)
        seen = []
        cursor = None
        pages = 0
        while True:
            page = job.client.list_page(cursor=cursor, limit=10)
            pages += 1
            seen.extend(order.id for order in page.items)
            if not page.has_more:
                break
            cursor = page.next_cursor
        self.assertEqual(pages, 3)
        self.assertEqual(len(seen), 24)
        self.assertEqual(len(set(seen)), 24)
        self.assertEqual(seen, [f"ORD-{1000 + i}" for i in range(1, 25)])

    def test_resume_from_cursor_skips_earlier_rows(self):
        job, *_ = make_job(page_limit=5)
        first = job.client.list_page(limit=5)
        rest_ids = []
        cursor = first.next_cursor
        while cursor:
            page = job.client.list_page(cursor=cursor, limit=5)
            rest_ids.extend(order.id for order in page.items)
            cursor = page.next_cursor if page.has_more else None
        self.assertEqual(first.items[-1].id, "ORD-1005")
        self.assertNotIn("ORD-1005", rest_ids)
        self.assertEqual(len(rest_ids), 19)

    def test_invalid_cursor_is_a_non_retryable_bad_request(self):
        job, service, sleeper, logger, clock, transport = make_job()
        with self.assertRaises(ApiError) as ctx:
            job.client.list_page(cursor="%%%not-a-cursor%%%")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "bad_request")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(transport.retry_count, 0)

    def test_truncated_envelope_raises_schema_error(self):
        job, *_ = make_job(
            faults=[{"method": "GET", "path": "/v1/orders", "truncate_envelope": True}]
        )
        with self.assertRaises(SchemaError):
            job.client.list_page()

    def test_malformed_json_raises_schema_error(self):
        job, *_ = make_job(faults=[{"method": "GET", "path": "/v1/orders", "malformed": True}])
        with self.assertRaises(SchemaError):
            job.client.list_page()

    def test_nan_in_a_page_body_raises_schema_error(self):
        body = '{"object":"list","items":[],"next_cursor":null,"has_more":false,"limit":NaN}'
        job, *_ = make_job(faults=[{"method": "GET", "path": "/v1/orders", "raw_body": body}])
        with self.assertRaises(SchemaError):
            job.client.list_page()

    def test_new_order_after_cursor_appears_on_incremental_list(self):
        catalog = build_catalog()
        job, service, *_ = make_job(catalog=catalog, page_limit=50)
        page = job.client.list_page(limit=50)
        self.assertFalse(page.has_more)
        cursor = encode_cursor(page.items[-1].updated_at, page.items[-1].id)
        service.upsert_order(
            {
                "id": "ORD-1090",
                "status": "pending",
                "amount_cents": 100,
                "currency": "USD",
                "updated_at": "2026-01-03T00:00:00Z",
                "version": 1,
                "customer_ref": "CUST-900",
                "items": [{"sku": "SKU-900", "qty": 1, "unit_cents": 100}],
            }
        )
        nxt = job.client.list_page(cursor=cursor, limit=50)
        self.assertEqual([order.id for order in nxt.items], ["ORD-1090"])

    def test_short_page_is_logged_when_source_returns_fewer_than_limit(self):
        clock = ManualClock()
        logger = JsonLogger()
        catalog = build_catalog()[:3]
        service = MockFulfillmentApi(catalog, token=LAB_TOKEN)
        client, _ = build_client(
            service,
            logger=logger,
            clock=clock,
            sleeper=RecordingSleeper(clock),
            policy=RetryPolicy(max_attempts=1),
            default_limit=10,
        )
        # A final page smaller than the limit is normal, not short.
        page = client.list_page(limit=10)
        self.assertFalse(page.has_more)
        self.assertFalse(page.short_page)

        # Simulate a source that drops rows but still claims has_more.
        def shrink(request, request_id):
            payload = {
                "object": "list",
                "items": catalog[:2],
                "next_cursor": "keep-going",
                "has_more": True,
                "limit": 10,
            }
            return service._json(200, payload, request_id=request_id)

        service._list_orders = shrink  # type: ignore[method-assign]
        page = client.list_page(limit=10)
        self.assertTrue(page.short_page)
        self.assertEqual(logger.of_type("short_page")[-1]["item_count"], 2)


if __name__ == "__main__":
    unittest.main()
