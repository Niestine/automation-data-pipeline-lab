import unittest

import helpers  # noqa: F401

from web_collection_lab.errors import HttpError, TransportError
from web_collection_lab.retry import (
    RetryPolicy,
    backoff_ms,
    delay_from_headers,
    is_retryable_error,
    is_retryable_request,
    is_retryable_status,
    rng_for,
)
from web_collection_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper
from web_collection_lab.transport import HttpRequest, HttpResponse, RetryingTransport, raise_for_status


class FakeInner:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def send(self, request):
        self.calls += 1
        item = self.statuses[self.calls - 1]
        if item == "timeout":
            raise TransportError("timeout", "boom")
        status, headers = item
        return HttpResponse(status=status, headers=headers, body=b"ok")


class RetryTests(unittest.TestCase):
    def test_backoff_doubles_until_cap(self):
        policy = RetryPolicy(base_delay_ms=10, max_delay_ms=50, multiplier=2.0, jitter_ms=0)
        rng = rng_for(7, "GET", "/catalog")
        delays = [backoff_ms(policy, attempt, rng) for attempt in range(1, 6)]
        self.assertEqual(delays, [10, 20, 40, 50, 50])

    def test_jitter_is_deterministic_for_the_same_seed(self):
        policy = RetryPolicy(base_delay_ms=10, jitter_ms=3)
        first = [backoff_ms(policy, n, rng_for(7, n, "GET")) for n in (1, 2, 3)]
        second = [backoff_ms(policy, n, rng_for(7, n, "GET")) for n in (1, 2, 3)]
        self.assertEqual(first, second)
        for base, delay in zip((10, 20, 40), first):
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, base + 3)

    def test_retry_after_seconds_and_ms_headers(self):
        self.assertEqual(delay_from_headers({"retry-after": "2"}), 2000)
        self.assertEqual(delay_from_headers({"x-retry-after-ms": "25"}), 25)
        self.assertIsNone(delay_from_headers({}))

    def test_non_ascii_or_http_date_retry_after_is_ignored(self):
        self.assertIsNone(delay_from_headers({"retry-after": "²"}))
        self.assertIsNone(delay_from_headers({"x-retry-after-ms": "١٢"}))
        self.assertIsNone(delay_from_headers({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))

    def test_huge_retry_after_is_capped_by_policy(self):
        clock = ManualClock()
        sleeper = RecordingSleeper(clock)
        inner = FakeInner([(503, {"retry-after": "86400"}), (200, {})])
        transport = RetryingTransport(
            inner,
            policy=RetryPolicy(max_retry_after_ms=5_000),
            logger=JsonLogger(),
            clock=clock,
            sleeper=sleeper,
        )
        self.assertEqual(transport.send(HttpRequest("GET", "/catalog")).status, 200)
        self.assertEqual(sleeper.delays, [5_000])

    def test_repeated_timeouts_reraise_after_max_attempts(self):
        clock = ManualClock()
        sleeper = RecordingSleeper(clock)
        inner = FakeInner(["timeout"] * 3)
        transport = RetryingTransport(
            inner,
            policy=RetryPolicy(max_attempts=3, jitter_ms=0),
            logger=JsonLogger(),
            clock=clock,
            sleeper=sleeper,
        )
        with self.assertRaises(TransportError):
            transport.send(HttpRequest("GET", "/catalog"))
        self.assertEqual(inner.calls, 3)
        self.assertEqual(sleeper.delays, [10, 20])

    def test_exhausted_status_retries_return_the_last_response(self):
        clock = ManualClock()
        inner = FakeInner([(503, {})] * 2)
        transport = RetryingTransport(
            inner,
            policy=RetryPolicy(max_attempts=2, jitter_ms=0),
            logger=JsonLogger(),
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )
        response = transport.send(HttpRequest("GET", "/catalog"))
        self.assertEqual(response.status, 503)
        self.assertEqual(inner.calls, 2)
        with self.assertRaises(HttpError) as ctx:
            raise_for_status(response)
        self.assertTrue(ctx.exception.retryable)

    def test_status_classification(self):
        self.assertTrue(is_retryable_status(429))
        self.assertTrue(is_retryable_status(503))
        self.assertFalse(is_retryable_status(401))
        self.assertFalse(is_retryable_status(400))
        self.assertFalse(is_retryable_status(404))
        self.assertFalse(is_retryable_status(422))

    def test_post_retries_only_with_idempotency_key(self):
        self.assertTrue(is_retryable_request("GET", {}))
        self.assertFalse(is_retryable_request("POST", {}))
        self.assertTrue(is_retryable_request("POST", {"idempotency-key": "k1"}))

    def test_timeout_is_retryable_transport_error(self):
        self.assertTrue(is_retryable_error(TransportError("timeout", "x")))
        self.assertFalse(is_retryable_error(TransportError("tls", "x")))

    def test_get_is_retried_on_503_and_429(self):
        clock = ManualClock()
        sleeper = RecordingSleeper(clock)
        inner = FakeInner([(503, {}), (429, {"retry-after": "0"}), (200, {})])
        transport = RetryingTransport(
            inner, policy=RetryPolicy(jitter_ms=0), logger=JsonLogger(), clock=clock, sleeper=sleeper, seed=7
        )
        response = transport.send(HttpRequest("GET", "/catalog"))
        self.assertEqual(response.status, 200)
        self.assertEqual(transport.retry_count, 2)
        self.assertEqual(inner.calls, 3)
        self.assertEqual(sleeper.delays[1], 0)

    def test_404_is_not_retried(self):
        clock = ManualClock()
        inner = FakeInner([(404, {}), (200, {})])
        transport = RetryingTransport(
            inner,
            policy=RetryPolicy(max_attempts=4, jitter_ms=0),
            logger=JsonLogger(),
            clock=clock,
            sleeper=RecordingSleeper(clock),
        )
        response = transport.send(HttpRequest("GET", "/products/missing"))
        self.assertEqual(response.status, 404)
        self.assertEqual(inner.calls, 1)


if __name__ == "__main__":
    unittest.main()
