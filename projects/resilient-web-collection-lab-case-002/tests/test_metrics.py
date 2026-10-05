import helpers  # noqa: F401
import unittest

from incremental_crawl_lab.metrics import age_at, freshness_at, time_average


class MetricsTest(unittest.TestCase):
    def test_left_state_integral_for_two_edits(self) -> None:
        syncs = [0.0]
        changes = [2.0, 5.0]
        self.assertEqual(freshness_at(syncs, changes, 9.0), 0)
        self.assertEqual(age_at(syncs, changes, 9.0), 7.0)
        freshness, age = time_average(syncs, changes, 0.0, 10.0)
        self.assertAlmostEqual(freshness, 0.2)
        self.assertAlmostEqual(age, 3.2)

    def test_resync_limits_the_stale_interval(self) -> None:
        freshness, age = time_average([0.0, 6.0], [2.0, 5.0], 0.0, 10.0)
        self.assertAlmostEqual(freshness, 0.6)
        self.assertAlmostEqual(age, 0.8)

    def test_time_before_the_first_sync_is_outside_the_window(self) -> None:
        freshness, age = time_average([5.0], [], 0.0, 10.0)
        self.assertAlmostEqual(freshness, 1.0)
        self.assertAlmostEqual(age, 0.0)


if __name__ == "__main__":
    unittest.main()
