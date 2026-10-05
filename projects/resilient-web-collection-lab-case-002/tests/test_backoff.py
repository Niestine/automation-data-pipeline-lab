import helpers  # noqa: F401
import random
import unittest
from datetime import datetime, timezone

from incremental_crawl_lab.backoff import equal_jitter, full_jitter, no_jitter, parse_retry_after, retry_delay


class BackoffTest(unittest.TestCase):
    def test_no_jitter_is_identical_for_every_client(self) -> None:
        values = [no_jitter(3, 1.0, 300.0) for _ in range(100)]
        self.assertEqual(values, [8.0] * 100)

    def test_full_jitter_stays_inside_the_cap_and_spreads(self) -> None:
        attempt = 8
        cap = 300.0
        temp = min(cap, 1.0 * (2**attempt))
        values = [full_jitter(attempt, random.Random(i), 1.0, cap) for i in range(100)]
        self.assertTrue(all(0.0 <= value <= temp for value in values))
        self.assertGreater(len({round(value, 6) for value in values}), 1)
        self.assertTrue(all(value <= cap for value in values))

    def test_retry_after_is_a_lower_bound(self) -> None:
        for seed in range(100):
            delay = retry_delay(1, random.Random(seed), base=1.0, cap=300.0, retry_after_seconds=30)
            self.assertGreaterEqual(delay, 30.0)

    def test_http_date_retry_after(self) -> None:
        now = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
        delay = parse_retry_after("Tue, 06 Oct 2026 00:00:00 GMT", now)
        self.assertEqual(delay, 86400.0)
        self.assertIsNone(parse_retry_after("soon", now))
        self.assertIsNone(parse_retry_after("", now))
        self.assertEqual(parse_retry_after("Mon, 05 Oct 2026 00:00:00 GMT", now + 5), 0.0)

    def test_equal_jitter_foil_stays_in_the_upper_half(self) -> None:
        attempt = 4
        temp = min(300.0, 1.0 * (2**attempt))
        for seed in range(40):
            value = equal_jitter(attempt, random.Random(seed), 1.0, 300.0)
            self.assertGreaterEqual(value, temp / 2.0)
            self.assertLessEqual(value, temp)


if __name__ == "__main__":
    unittest.main()
