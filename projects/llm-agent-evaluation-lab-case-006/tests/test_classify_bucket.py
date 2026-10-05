"""Vendor classification and the retry token bucket."""

from __future__ import annotations

import unittest

from helpers import SeqRNG
from tide_route.bucket import RetryBucket, retry_wait_ms
from tide_route.classify import classify
from tide_route.problems import core_view, make_problem


def decision(**kwargs):
    base = dict(
        vendor="openai",
        status=500,
        error_code=None,
        error_type=None,
        headers=None,
        message="secret-key-should-not-leak",
    )
    base.update(kwargs)
    return classify(**base)


class ClassifyTests(unittest.TestCase):
    def test_openai_billing_code_beats_insufficient_quota_type(self):
        for code in (
            "credit_balance_exhausted",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
        ):
            found = decision(
                status=429,
                error_code=code,
                error_type="insufficient_quota",
                message="rate limit wording is not the key",
            )
            self.assertEqual(found.failure_class, "terminal_billing")
            self.assertFalse(found.retryable)
            self.assertEqual(found.blocks, "credential")

    def test_detail_prose_is_not_the_lookup_key(self):
        left = decision(status=429, error_code="credit_balance_exhausted", message="alpha")
        right = decision(status=429, error_code="credit_balance_exhausted", message="beta")
        self.assertEqual(left.failure_class, right.failure_class)

    def test_slow_down_is_rate_limit_and_503_is_not(self):
        slow = decision(status=429, error_code="slow_down", error_type="rate_limit_error")
        overloaded = decision(
            status=503,
            error_code="server_is_overloaded",
            error_type="service_unavailable_error",
        )
        self.assertEqual(slow.failure_class, "rate_limited")
        self.assertEqual(overloaded.failure_class, "overloaded")
        self.assertNotEqual(overloaded.http_status, 429)
        self.assertEqual(overloaded.http_status, 503)

    def test_openai_500_is_transient(self):
        found = decision(status=500)
        self.assertEqual(found.failure_class, "transient")
        self.assertEqual(found.retry_kind, "transient")

    def test_claude_spend_limit_and_billing(self):
        spend = decision(vendor="anthropic", status=400, spend_limit=True)
        billing = decision(vendor="anthropic", status=402, error_type="billing_error")
        self.assertEqual(spend.failure_class, "terminal_billing")
        self.assertEqual(billing.failure_class, "terminal_billing")
        self.assertFalse(spend.retryable)

    def test_claude_bare_429_escalates_on_the_second_hit(self):
        first = decision(vendor="anthropic", status=429, error_type="rate_limit_error")
        second = decision(
            vendor="anthropic",
            status=429,
            error_type="rate_limit_error",
            bare_429_seen=True,
        )
        headed = decision(
            vendor="anthropic",
            status=429,
            error_type="rate_limit_error",
            headers={"retry-after": "2"},
            bare_429_seen=True,
        )
        self.assertTrue(first.retryable)
        self.assertEqual(first.lab_rule, "first_bare_429")
        self.assertEqual(second.failure_class, "terminal_billing")
        self.assertEqual(headed.failure_class, "rate_limited")
        self.assertTrue(headed.retryable)

    def test_openai_bare_429_does_not_escalate(self):
        found = decision(status=429, bare_429_seen=True)
        self.assertEqual(found.failure_class, "rate_limited")
        self.assertTrue(found.retryable)

    def test_claude_413_529_409_504(self):
        too_big = decision(vendor="anthropic", status=413, error_type="request_too_large")
        overloaded = decision(vendor="anthropic", status=529, error_type="overloaded_error")
        conflict = decision(vendor="anthropic", status=409, error_type="conflict_error")
        timed = decision(vendor="anthropic", status=504, error_type="timeout_error")
        self.assertEqual(too_big.failure_class, "request_defect")
        self.assertEqual(too_big.blocks, "all")
        self.assertEqual(overloaded.failure_class, "overloaded")
        self.assertNotEqual(overloaded.failure_class, "request_defect")
        self.assertTrue(overloaded.failover_allowed)
        self.assertEqual(conflict.failure_class, "conflict")
        self.assertFalse(conflict.retryable)
        self.assertEqual(timed.failure_class, "timeout")
        self.assertFalse(timed.retryable)

    def test_aws_code_beats_status(self):
        timeout = decision(vendor="aws", status=400, error_code="RequestTimeout")
        invalid = decision(vendor="aws", status=400, error_code="ValidationException")
        throttled = decision(vendor="aws", status=503, error_code="ThrottlingException")
        self.assertTrue(timeout.retryable)
        self.assertEqual(timeout.retry_kind, "transient")
        self.assertEqual(invalid.failure_class, "request_defect")
        self.assertFalse(invalid.retryable)
        self.assertEqual(throttled.retry_kind, "throttling")

    def test_unrecognized_499_is_not_retried(self):
        found = decision(vendor="", status=499)
        self.assertEqual(found.failure_class, "request_defect")
        self.assertFalse(found.retryable)

    def test_integer_retry_after_only(self):
        seconds, parsed = __import__(
            "tide_route.classify", fromlist=["parse_retry_after"]
        ).parse_retry_after({"Retry-After": "2"})
        self.assertEqual((seconds, parsed), (2, "integer"))
        seconds, parsed = __import__(
            "tide_route.classify", fromlist=["parse_retry_after"]
        ).parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        self.assertEqual(parsed, "absent_or_unparsed")
        self.assertIsNone(seconds)

    def test_openai_sdk_exception_types_are_both_handled(self):
        limited = decision(status=0, error_type="RateLimitError")
        server = decision(status=0, error_type="InternalServerError")
        self.assertEqual(limited.failure_class, "rate_limited")
        self.assertEqual(limited.retry_kind, "throttling")
        self.assertEqual(server.failure_class, "transient")
        self.assertEqual(server.retry_kind, "transient")

    def test_non_ascii_digit_retry_after_is_unparsed(self):
        from tide_route.classify import parse_retry_after

        self.assertEqual(
            parse_retry_after({"Retry-After": "²"}),
            (None, "absent_or_unparsed"),
        )

    def test_problem_detail_does_not_change_class(self):
        left = make_problem(
            "budget-exhausted",
            detail="one",
            instance="https://portfolio.example/instances/tide-0001",
            failure_class="budget_exhausted",
            budget_remaining=1.0,
        )
        right = make_problem(
            "budget-exhausted",
            detail="two",
            instance="https://portfolio.example/instances/tide-0002",
            failure_class="budget_exhausted",
            budget_remaining=1.0,
        )
        self.assertEqual(left["type"], right["type"])
        self.assertEqual(left["failure_class"], right["failure_class"])
        self.assertEqual(left["status"], 402)
        self.assertNotEqual(left["detail"], right["detail"])
        viewed = core_view(left)
        self.assertEqual(set(viewed), {"type", "status"})
        self.assertEqual(core_view({"status": 402})["type"], "about:blank")


class BucketTests(unittest.TestCase):
    def test_first_try_success_stays_at_capacity(self):
        bucket = RetryBucket()
        bucket.on_first_try_success()
        self.assertEqual(bucket.balance, 500)

    def test_failed_retry_then_successful_retry_nets_minus_14(self):
        bucket = RetryBucket()
        first = bucket.charge("transient")
        bucket.charge("transient")
        bucket.on_retry_success(bucket.cost_of("transient"))
        self.assertEqual(first, 14)
        self.assertEqual(bucket.balance, 500 - 14)

    def test_empty_bucket_cannot_pay(self):
        bucket = RetryBucket(balance=0)
        self.assertFalse(bucket.can_pay("transient"))

    def test_retry_after_is_a_minimum_and_is_not_clamped(self):
        wait, parsed = retry_wait_ms(SeqRNG([0.0]), 0, "throttling", 30, "integer")
        self.assertEqual(parsed, "integer")
        self.assertGreaterEqual(wait, 30000.0)

    def test_rfc9110_archive_gap_non_integer_retry_after_uses_jitter(self):
        wait, parsed = retry_wait_ms(
            SeqRNG([0.0]), 0, "transient", None, "absent_or_unparsed"
        )
        self.assertEqual(parsed, "absent_or_unparsed")
        self.assertEqual(wait, 0.0)

    def test_throttling_base_is_longer_than_transient(self):
        throttled, _parsed = retry_wait_ms(SeqRNG([0.999]), 0, "throttling", None, "absent_or_unparsed")
        transient, _parsed = retry_wait_ms(SeqRNG([0.999]), 0, "transient", None, "absent_or_unparsed")
        self.assertGreater(throttled, 50.0)
        self.assertLess(throttled, 1000.0)
        self.assertLess(transient, 50.0)


if __name__ == "__main__":
    unittest.main()
