import unittest

import helpers  # noqa: F401  (adds src/ to sys.path)
from llm_agent_lab.retry import RetryPolicy, backoff_ms, is_retryable, rng_for


class RetryTests(unittest.TestCase):
    def test_backoff_doubles_until_cap(self):
        policy = RetryPolicy(base_delay_ms=10, max_delay_ms=50, multiplier=2.0, jitter_ms=0)
        rng = rng_for("T-1001", 7)
        delays = [backoff_ms(policy, attempt, rng) for attempt in range(1, 6)]
        self.assertEqual(delays, [10, 20, 40, 50, 50])

    def test_jitter_is_deterministic_for_same_seed_and_bounded(self):
        policy = RetryPolicy(base_delay_ms=10, jitter_ms=3)
        first = [backoff_ms(policy, n, rng_for("T-1001", 7)) for n in (1, 2, 3)]
        second = [backoff_ms(policy, n, rng_for("T-1001", 7)) for n in (1, 2, 3)]
        self.assertEqual(first, second)
        for base, delay in zip((10, 20, 40), first):
            self.assertGreaterEqual(delay, base)
            self.assertLessEqual(delay, base + 3)

    def test_parse_and_schema_errors_are_retryable(self):
        policy = RetryPolicy()
        self.assertTrue(is_retryable("parse_error", transient=False, policy=policy))
        self.assertTrue(is_retryable("schema_error", transient=False, policy=policy))

    def test_transient_errors_are_retryable(self):
        policy = RetryPolicy()
        self.assertTrue(is_retryable("timeout", transient=True, policy=policy))

    def test_policy_denials_are_not_retryable(self):
        policy = RetryPolicy()
        self.assertFalse(is_retryable("prompt_injection", transient=False, policy=policy))
        self.assertFalse(is_retryable("policy_violation", transient=True, policy=policy))
        self.assertFalse(is_retryable("auth", transient=False, policy=policy))

    def test_single_attempt_policy_never_retries(self):
        policy = RetryPolicy(max_attempts=1)
        self.assertFalse(is_retryable("parse_error", transient=True, policy=policy))

    def test_unknown_non_transient_code_is_not_retryable(self):
        policy = RetryPolicy()
        self.assertFalse(is_retryable("quota_exhausted", transient=False, policy=policy))
        self.assertFalse(is_retryable("bad_script", transient=False, policy=policy))


if __name__ == "__main__":
    unittest.main()
