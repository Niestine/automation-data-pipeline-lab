import unittest

from brief_router_lab.retry import CircuitBreaker


class CircuitTests(unittest.TestCase):
    def test_opens_after_threshold(self):
        breaker = CircuitBreaker(failure_threshold=3, cooldown_ms=500)
        now = 1_000_000
        breaker.record_failure("catalog.get", now)
        breaker.record_failure("catalog.get", now)
        self.assertTrue(breaker.allow("catalog.get", now))
        breaker.record_failure("catalog.get", now)
        self.assertTrue(breaker.is_open("catalog.get"))
        self.assertFalse(breaker.allow("catalog.get", now + 10))

    def test_success_resets(self):
        breaker = CircuitBreaker(failure_threshold=3, cooldown_ms=500)
        now = 1_000_000
        breaker.record_failure("catalog.get", now)
        breaker.record_success("catalog.get")
        self.assertEqual(breaker.failures.get("catalog.get"), 0)
        self.assertTrue(breaker.allow("catalog.get", now))

    def test_cooldown_half_open(self):
        breaker = CircuitBreaker(failure_threshold=2, cooldown_ms=500)
        now = 1_000_000
        breaker.record_failure("kb.get", now)
        breaker.record_failure("kb.get", now)
        self.assertFalse(breaker.allow("kb.get", now + 100))
        self.assertTrue(breaker.allow("kb.get", now + 500))
        self.assertFalse(breaker.is_open("kb.get"))
