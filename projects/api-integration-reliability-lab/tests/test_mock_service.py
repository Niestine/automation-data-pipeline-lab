import unittest

import helpers  # noqa: F401
from helpers import make_job

from api_reliability_lab.errors import ApiError, AuthError
from api_reliability_lab.models import LAB_TOKEN
from api_reliability_lab.transport import HttpRequest


class MockServiceTests(unittest.TestCase):
    def test_health_is_open_and_orders_require_bearer_token(self):
        job, *_ = make_job()
        self.assertEqual(job.client.health()["status"], "ok")
        response = job.client.transport.inner.send(
            HttpRequest("GET", "/v1/orders", query={"limit": "1"})
        )
        self.assertEqual(response.status, 401)

    def test_unknown_route_is_404(self):
        job, *_ = make_job()
        response = job.client.transport.send(
            HttpRequest(
                "GET",
                "/v1/nope",
                headers={"authorization": f"Bearer {LAB_TOKEN}"},
            )
        )
        self.assertEqual(response.status, 404)

    def test_get_order_and_missing_order(self):
        job, *_ = make_job()
        order = job.client.get_order("ORD-1001")
        self.assertEqual(order.id, "ORD-1001")
        with self.assertRaises(ApiError) as ctx:
            job.client.get_order("ORD-9999")
        self.assertEqual(ctx.exception.status, 404)

    def test_wrong_token_raises_auth_error(self):
        job, *_ = make_job()
        job.client.token = "other"
        with self.assertRaises(AuthError):
            job.client.list_page()


if __name__ == "__main__":
    unittest.main()
