"""RFC 6298 estimator: floor, order, granularity, doubling, and the TCP preset."""

from __future__ import annotations

import unittest

from bay_notice.policy import application_policy, tcp_6298_policy
from bay_notice.timer import RtoEstimator


class TimerTests(unittest.TestCase):
    def test_no_sample_table_doubles_then_hits_the_ceiling(self) -> None:
        estimator = RtoEstimator(floor_s=1, ceiling_s=6, granularity_s=0.01)
        seen = []
        for _ in range(4):
            seen.append(estimator.rto)
            estimator.on_timeout()
        self.assertEqual(seen, [1.0, 2.0, 4.0, 6.0])

    def test_tcp_preset_floor_and_ceiling(self) -> None:
        policy = tcp_6298_policy()
        self.assertEqual(policy.floor_s, 1.0)
        self.assertGreaterEqual(policy.ceiling_s, 60.0)
        estimator = RtoEstimator.from_policy(policy)
        armed = []
        for _ in range(8):
            armed.append(estimator.rto)
            estimator.on_timeout()
        self.assertEqual(armed[0], 1.0)
        self.assertEqual(armed[-1], 60.0)
        self.assertLessEqual(max(armed), 60.0)

    def test_application_profile_is_tighter_than_the_tcp_floor(self) -> None:
        policy = application_policy()
        self.assertLess(policy.floor_s, 1.0)
        self.assertGreater(policy.ceiling_s, policy.floor_s)

    def test_first_sample_uses_the_half_variance_rule(self) -> None:
        estimator = RtoEstimator(floor_s=1, ceiling_s=60, granularity_s=0.05)
        self.assertTrue(estimator.observe(1.0, True))
        self.assertEqual(estimator.srtt, 1.0)
        self.assertEqual(estimator.rttvar, 0.5)
        self.assertEqual(estimator.rto, 3.0)

    def test_sub_floor_rto_is_rounded_up(self) -> None:
        estimator = RtoEstimator(floor_s=1, ceiling_s=60, granularity_s=0.01)
        estimator.observe(0.25, True)
        self.assertEqual(estimator.srtt, 0.25)
        self.assertEqual(estimator.rttvar, 0.125)
        self.assertEqual(estimator.rto, 1.0)

    def test_later_sample_updates_variance_before_the_mean(self) -> None:
        estimator = RtoEstimator(floor_s=0.01, ceiling_s=30, granularity_s=0.001)
        estimator.observe(1.0, True)
        estimator.observe(3.0, True)
        self.assertEqual(estimator.rttvar, 0.875)
        self.assertEqual(estimator.srtt, 1.25)
        swapped = (1.0 - 0.25) * 0.5 + 0.25 * abs(1.25 - 3.0)
        self.assertNotEqual(estimator.rttvar, swapped)

    def test_zero_variance_term_uses_granularity(self) -> None:
        estimator = RtoEstimator(floor_s=0.1, ceiling_s=10, granularity_s=0.25)
        estimator.observe(0.0, True)
        self.assertEqual(estimator.rttvar, 0.0)
        self.assertEqual(estimator.rto, estimator.srtt + estimator.granularity_s)

    def test_ambiguous_sample_does_not_move_the_estimator(self) -> None:
        estimator = RtoEstimator(floor_s=1, ceiling_s=60, granularity_s=0.05)
        estimator.observe(1.0, True)
        snapshot = (estimator.srtt, estimator.rttvar, estimator.rto)
        self.assertFalse(estimator.observe(9.0, False))
        self.assertEqual((estimator.srtt, estimator.rttvar, estimator.rto), snapshot)

    def test_repeated_backoff_clears_the_estimate_for_the_next_sample(self) -> None:
        estimator = RtoEstimator(floor_s=0.1, ceiling_s=30, granularity_s=0.001, reset_after=2)
        estimator.observe(1.0, True)
        estimator.on_timeout()
        self.assertEqual(estimator.srtt, 1.0)
        estimator.on_timeout()
        self.assertIsNone(estimator.srtt)
        self.assertIsNone(estimator.rttvar)
        estimator.observe(0.5, True)
        self.assertEqual(estimator.srtt, 0.5)
        self.assertEqual(estimator.rttvar, 0.25)

    def test_a_fresh_sample_pulls_a_backed_off_rto_back_down(self) -> None:
        estimator = RtoEstimator(floor_s=1, ceiling_s=60, granularity_s=0.05)
        for _ in range(8):
            estimator.on_timeout()
        self.assertEqual(estimator.rto, 60.0)
        estimator.observe(1.0, True)
        self.assertEqual(estimator.rto, 3.0)


if __name__ == "__main__":
    unittest.main()
