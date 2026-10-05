"""Overload cliff and the error-avoiding balancer."""

from __future__ import annotations

import unittest

from bay_notice.capacity import CapacityModel, retry_offered_load


class CapacityTests(unittest.TestCase):
    def test_collapse_persists_at_0_9c_and_clears_near_0_1c(self) -> None:
        model = CapacityModel(capacity=10000, instances=100)
        self.assertEqual(model.offer(10000), "stable")
        self.assertFalse(model.crash_loop)
        self.assertEqual(model.offer(11000), "collapse")
        self.assertTrue(model.crash_loop)
        self.assertEqual(model.healthy_fraction, 0.1)
        self.assertEqual(model.offer(9000), "crash-loop")
        self.assertTrue(model.crash_loop)
        self.assertEqual(model.offer(1000), "recovered")
        self.assertFalse(model.crash_loop)
        self.assertEqual(model.healthy_fraction, 1.0)

    def test_error_avoiding_balancer_stabilizes_at_a_lower_load(self) -> None:
        keep = CapacityModel()
        avoid = CapacityModel()
        avoid.avoid_errors = True
        avoid.note_injected_errors(10)
        self.assertEqual(keep.highest_stable_load(), 10999)
        self.assertEqual(avoid.highest_stable_load(), 9899)
        self.assertLess(avoid.highest_stable_load(), keep.highest_stable_load())
        self.assertEqual(keep.offer(10000), "stable")
        self.assertEqual(avoid.offer(10000), "collapse")

    def test_standard_retry_crosses_the_cliff_a_single_attempt_still_serves(self) -> None:
        base = 10000
        quiet = CapacityModel()
        storm = CapacityModel()
        self.assertEqual(quiet.offer(retry_offered_load(base, 1.0)), "stable")
        self.assertEqual(storm.offer(retry_offered_load(base, 1.2)), "collapse")
        self.assertTrue(storm.crash_loop)
        self.assertFalse(quiet.crash_loop)


if __name__ == "__main__":
    unittest.main()
