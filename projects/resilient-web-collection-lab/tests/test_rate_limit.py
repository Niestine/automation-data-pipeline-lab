import unittest

import helpers  # noqa: F401

from web_collection_lab.models import LAB_NOW_MS
from web_collection_lab.rate_limit import RateLimiter
from web_collection_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper


class RateLimitTests(unittest.TestCase):
    def _limiter(self, min_interval_ms, burst=1):
        clock = ManualClock()
        sleeper = RecordingSleeper(clock)
        limiter = RateLimiter(
            min_interval_ms=min_interval_ms,
            burst=burst,
            clock=clock,
            sleeper=sleeper,
            logger=JsonLogger(),
        )
        return limiter, sleeper, clock

    def test_zero_interval_never_waits(self):
        limiter, sleeper, _clock = self._limiter(0)
        self.assertEqual(limiter.wait(), 0)
        self.assertEqual(limiter.wait(), 0)
        self.assertEqual(sleeper.delays, [])

    def test_second_request_waits_the_interval(self):
        limiter, sleeper, clock = self._limiter(1000)
        self.assertEqual(limiter.wait(), 0)
        waited = limiter.wait()
        self.assertEqual(waited, 1000)
        self.assertEqual(sleeper.delays, [1000])
        self.assertEqual(clock.now_ms(), LAB_NOW_MS + 1000)

    def test_burst_allows_immediate_front_load(self):
        limiter, sleeper, _clock = self._limiter(1000, burst=2)
        self.assertEqual(limiter.wait(), 0)
        self.assertEqual(limiter.wait(), 0)
        self.assertEqual(limiter.wait(), 1000)
        self.assertEqual(sleeper.delays, [1000])

    def test_elapsed_time_reduces_wait(self):
        limiter, sleeper, clock = self._limiter(1000)
        limiter.wait()
        clock.advance_ms(400)
        waited = limiter.wait()
        self.assertEqual(waited, 600)
        self.assertEqual(sleeper.delays, [600])

    def test_fractional_token_wait_rounds_up(self):
        limiter, sleeper, clock = self._limiter(3)
        limiter.wait()
        clock.advance_ms(1)
        # 2/3 of a token is missing: 2 ms, not int(1.99...) = 1 ms.
        self.assertEqual(limiter.wait(), 2)
        self.assertEqual(sleeper.delays, [2])

    def test_set_min_interval_applies_to_later_waits(self):
        limiter, sleeper, _clock = self._limiter(0)
        limiter.wait()
        limiter.set_min_interval_ms(250)
        limiter.wait()
        self.assertEqual(sleeper.delays, [250])


if __name__ == "__main__":
    unittest.main()
