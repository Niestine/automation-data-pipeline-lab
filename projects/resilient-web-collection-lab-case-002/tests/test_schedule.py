import helpers  # noqa: F401
import math
import random
import unittest

from incremental_crawl_lab.estimate import (
    estimate_rate,
    expected_age,
    expected_freshness,
    time_average_freshness,
)
from incremental_crawl_lab.schedule import SchedItem, choose_batch, score_age, score_freshness, simulate_policies


class ScheduleTest(unittest.TestCase):
    def test_zero_rate_and_zero_age_score_nothing(self) -> None:
        self.assertEqual(score_freshness(0.0, 10.0, 7.0, 1.0), 0.0)
        self.assertEqual(score_freshness(0.1, 0.0, 7.0, 1.0), 0.0)
        self.assertEqual(score_age(0.0, 10.0, 1.0), 0.0)

    def test_audit_floor_visits_a_zero_rate_page(self) -> None:
        items = [SchedItem(f"z{i:02d}", 0.0, float(i)) for i in range(30)]
        items.append(SchedItem("hot", 5.0, 1000.0))
        picked = choose_batch(items, 10, "freshness", random.Random(1), 10.0, 7.0)
        keys = {item.key for item in picked}
        self.assertIn("z00", keys)
        self.assertIn("hot", keys)
        self.assertEqual(len(picked), 10)
        proportional = choose_batch(items, 10, "proportional", random.Random(1), 10.0, 7.0)
        self.assertIn("z00", {item.key for item in proportional})

    def test_estimator_censors(self) -> None:
        quiet = estimate_rate(0, 10, 3, 5)
        self.assertEqual(quiet.lam, 0.0)
        self.assertFalse(quiet.censored)
        censored = estimate_rate(1, 100, 1, 5)
        self.assertTrue(censored.censored)
        self.assertAlmostEqual(censored.lam, 0.2)
        partial = estimate_rate(1, 100, 2, 5)
        self.assertFalse(partial.censored)
        self.assertAlmostEqual(partial.lam, 0.01)

    def test_expected_curves_match_the_closed_forms(self) -> None:
        self.assertAlmostEqual(expected_freshness(0.0, 4.0), 1.0)
        self.assertAlmostEqual(expected_age(0.0, 4.0), 0.0)
        self.assertAlmostEqual(expected_freshness(1.0, 0.0), 1.0)
        # TODS closed forms at λ = 1, t = 1.
        self.assertAlmostEqual(expected_freshness(1.0, 1.0), math.exp(-1.0))
        self.assertAlmostEqual(expected_age(1.0, 1.0), 1.0 * (1.0 - (1.0 - math.exp(-1.0)) / 1.0))
        self.assertAlmostEqual(expected_age(0.2, 3.0), 3.0 * (1.0 - (1.0 - math.exp(-0.6)) / 0.6))
        self.assertAlmostEqual(time_average_freshness(1.0, 1.0), 1.0 - math.exp(-1.0))

    def test_scores_are_built_from_the_expected_curves(self) -> None:
        lam, tau, horizon, weight = 0.5, 2.0, 7.0, 0.5
        self.assertAlmostEqual(
            score_freshness(lam, tau, horizon, weight),
            weight * (1.0 - math.exp(-lam * tau)) * (1.0 - math.exp(-lam * horizon)) / (lam * horizon),
        )
        self.assertAlmostEqual(score_age(lam, tau, 1.0), tau - (1.0 - math.exp(-lam * tau)) / lam)
        # A page that changes many times per horizon scores low on freshness and high on age.
        self.assertLess(score_freshness(5.0, 7.0, 7.0, 1.0), score_freshness(0.1, 7.0, 7.0, 1.0))
        self.assertGreater(score_age(5.0, 7.0, 1.0), score_age(0.1, 7.0, 1.0))

    def test_eight_week_policy_ordering(self) -> None:
        report = simulate_policies()
        self.assertLess(report["proportional"]["freshness"], report["uniform"]["freshness"])
        self.assertGreaterEqual(report["freshness"]["freshness"], report["uniform"]["freshness"])
        self.assertLess(report["age"]["age"], report["freshness"]["age"])


if __name__ == "__main__":
    unittest.main()
