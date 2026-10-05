"""Single-tier and three-tier retry amplification."""

from __future__ import annotations

import unittest

from bay_notice.amplify import (
    always_fail_product,
    exact_half_load,
    expected_raf,
    shared_overloaded_calls,
    simulate_raf,
    tier_calls,
)
from bay_notice.budget import ReplayRandom, SharedBudget

SINGLE_JOBS = 20000
SINGLE_TOLERANCE = 0.03
CHAIN_JOBS = 8000
CHAIN_TOLERANCE = 0.2
SEED = 7


class RafTests(unittest.TestCase):
    def test_closed_form_matches_the_published_points(self) -> None:
        self.assertEqual(expected_raf(0.5, 3), 1.875)
        self.assertAlmostEqual(expected_raf(0.5, 3) ** 3, 6.591796875)
        single = expected_raf(0.3, 3)
        self.assertAlmostEqual(single, (1.0 - 0.3**4) / 0.7)
        self.assertAlmostEqual(single**3, 2.85, places=2)
        self.assertEqual(expected_raf(0.0, 3), 1.0)

    def test_exact_half_coin_load_is_1_875(self) -> None:
        calls, jobs = exact_half_load()
        self.assertEqual((calls, jobs), (30, 16))
        self.assertEqual(calls / jobs, 1.875)
        for retries in range(6):
            calls, jobs = exact_half_load(retries)
            self.assertEqual(calls / jobs, expected_raf(0.5, retries), retries)

    def test_seeded_single_tier_stays_inside_tolerance(self) -> None:
        observed = simulate_raf(SINGLE_JOBS, 0.5, 3, 1, SEED)
        self.assertAlmostEqual(observed, 1.875, delta=SINGLE_TOLERANCE)

    def test_zero_retry_raf_is_one_on_the_same_jobs(self) -> None:
        self.assertEqual(simulate_raf(SINGLE_JOBS, 0.5, 0, 1, SEED), 1.0)
        self.assertEqual(simulate_raf(CHAIN_JOBS, 0.5, 0, 3, SEED), 1.0)

    def test_three_tier_bound_for_the_published_points(self) -> None:
        for probability, bound_places in ((0.5, None), (0.3, 2)):
            expected = expected_raf(probability, 3) ** 3
            if probability == 0.5:
                self.assertAlmostEqual(expected, 6.591796875)
            else:
                self.assertAlmostEqual(expected, 2.85, places=bound_places)
            observed = simulate_raf(CHAIN_JOBS, probability, 3, 3, SEED)
            self.assertLessEqual(observed, expected + CHAIN_TOLERANCE)
            self.assertGreaterEqual(observed, expected - CHAIN_TOLERANCE)
            self.assertGreater(observed, 1.0)

    def test_uncoordinated_product_exceeds_the_shared_overloaded_path(self) -> None:
        self.assertEqual(always_fail_product(3, 4), 64)
        self.assertEqual(always_fail_product(2, 3), 9)
        self.assertEqual(shared_overloaded_calls(), 1)
        budget = SharedBudget(base_load=1, rng=ReplayRandom([]))
        self.assertEqual(tier_calls(3, 4, budget), 1)
        self.assertEqual(budget.refusals, 3)
        self.assertEqual(budget.admissions, 0)
        self.assertLess(shared_overloaded_calls(), always_fail_product(3, 4))
        outer = simulate_raf(CHAIN_JOBS, 0.5, 3, 1, SEED)
        chain = simulate_raf(CHAIN_JOBS, 0.5, 3, 3, SEED)
        self.assertLess(outer, chain)


if __name__ == "__main__":
    unittest.main()
