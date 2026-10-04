import unittest
from random import Random

from brief_router_lab.retry import RetryPolicy, backoff_ms, is_retryable, rng_for


class RetryTests(unittest.TestCase):
    def test_parse_error_retryable(self):
        self.assertTrue(is_retryable("parse_error", transient=False, policy=RetryPolicy()))

    def test_role_denied_not_retryable(self):
        self.assertFalse(is_retryable("role_denied", transient=False, policy=RetryPolicy()))

    def test_transient_flag_retryable(self):
        self.assertTrue(is_retryable("custom_blip", transient=True, policy=RetryPolicy()))

    def test_max_attempts_one_disables_retry(self):
        self.assertFalse(is_retryable("timeout", transient=True, policy=RetryPolicy(max_attempts=1)))

    def test_backoff_is_deterministic_for_seed(self):
        policy = RetryPolicy(jitter_ms=3)
        first = [backoff_ms(policy, attempt, rng_for("P-2001", 11)) for attempt in (1, 2, 3)]
        second = [backoff_ms(policy, attempt, rng_for("P-2001", 11)) for attempt in (1, 2, 3)]
        self.assertEqual(first, second)
        self.assertGreaterEqual(first[1], first[0])

    def test_rng_differs_by_packet(self):
        a = rng_for("P-2001", 11)
        b = rng_for("P-2002", 11)
        self.assertIsInstance(a, Random)
        self.assertNotEqual(a.random(), b.random())
