"""Jitter formulas and the token-endpoint classifier.

Each expected sleep is drawn from a second Random seeded the same way.
The test does not call the function under test twice.
"""

from __future__ import annotations

import random
import unittest

from helpers import LabCase
from lotcycle.params import TERMINAL_ERRORS
from lotcycle.retry import (
    classify_token_http,
    decorrelated_jitter,
    equal_jitter,
    full_jitter,
    no_jitter,
)


class JitterFormulaTest(unittest.TestCase):
    def test_full_jitter_matches_the_capped_draw(self) -> None:
        base, cap = 0.05, 2.0
        for attempt in range(5):
            ceiling = min(cap, base * (2**attempt))
            for seed in (1, 2, 9):
                expect = random.Random(seed).uniform(0.0, ceiling)
                got = full_jitter(random.Random(seed), attempt, base, cap)
                self.assertEqual(got, expect)
                self.assertGreaterEqual(got, 0.0)
                self.assertLess(got, ceiling)

    def test_equal_jitter_keeps_half_the_backoff(self) -> None:
        base, cap = 0.05, 2.0
        for attempt in (0, 3, 4):
            temp = min(cap, base * (2**attempt))
            expect = temp / 2.0 + random.Random(8).uniform(0.0, temp / 2.0)
            got = equal_jitter(random.Random(8), attempt, base, cap)
            self.assertEqual(got, expect)
            self.assertGreaterEqual(got, temp / 2.0)

    def test_decorrelated_jitter_uses_previous_sleep(self) -> None:
        base, cap = 0.05, 2.0
        previous = base
        expect_rng = random.Random(5)
        upper = previous * 3.0
        expect = min(cap, expect_rng.uniform(base, upper))
        got = decorrelated_jitter(random.Random(5), previous, base, cap)
        self.assertEqual(got, expect)

        # A previous sleep below base/3 would make the upper bound collapse.
        collapsed = decorrelated_jitter(random.Random(5), 0.001, base, cap)
        self.assertEqual(collapsed, base)

        wide = random.Random(3).uniform(base, 300.0)
        capped = decorrelated_jitter(random.Random(3), 100.0, base, cap)
        self.assertEqual(capped, min(cap, wide))
        self.assertLessEqual(capped, cap)

    def test_exponent_clamp_does_not_change_attempts_zero_through_four(self) -> None:
        for attempt in range(5):
            self.assertEqual(no_jitter(attempt, 0.05, 2.0), min(2.0, 0.05 * (2**attempt)))
        self.assertEqual(no_jitter(10, 0.05, 2.0), 2.0)
        self.assertEqual(no_jitter(62, 1.0, 10.0**18), no_jitter(80, 1.0, 10.0**18))


class TokenClassifierTest(LabCase):
    def test_terminal_oauth_errors_and_retriable_status(self) -> None:
        for error in (
            "invalid_request",
            "invalid_client",
            "invalid_grant",
            "unauthorized_client",
            "unsupported_grant_type",
            "invalid_scope",
        ):
            self.assertIn(error, TERMINAL_ERRORS)
            self.assertEqual(classify_token_http(400, error), "terminal")
        self.assertNotIn("temporarily_unavailable", TERMINAL_ERRORS)
        self.assertEqual(classify_token_http(400, "temporarily_unavailable"), "terminal")
        self.assertEqual(classify_token_http(503, "temporarily_unavailable"), "retry")
        self.assertEqual(classify_token_http(500, "server_error"), "retry")
        self.assertEqual(classify_token_http(200, None), "success")
        # A classified OAuth error stays terminal even when wrapped in HTTP 500.
        self.assertEqual(classify_token_http(500, "invalid_grant"), "terminal")


if __name__ == "__main__":
    unittest.main()
