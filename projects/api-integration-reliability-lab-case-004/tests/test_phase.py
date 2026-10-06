"""Proactive refresh phase and the one-slot contention comparison."""

from __future__ import annotations

import random
import unittest

import helpers  # noqa: F401  inserts the project src path
from lotcycle.params import ACCESS_LIFETIME
from lotcycle.retry import (
    proactive_delay,
    proactive_refresh_time,
    resume_after_outage,
    simulate_contention,
)


class PhaseTest(unittest.TestCase):
    def test_shared_lifetime_without_randomization(self) -> None:
        issued = 1_700_000_000.0
        times = [
            proactive_refresh_time(issued, None, ACCESS_LIFETIME, False) for _ in range(32)
        ]
        self.assertEqual(times, [issued + ACCESS_LIFETIME] * 32)
        held = [
            proactive_refresh_time(issued, random.Random(i), ACCESS_LIFETIME, False)
            for i in range(32)
        ]
        self.assertEqual(len(set(held)), 1)
        self.assertEqual(proactive_delay(None, ACCESS_LIFETIME, False), float(ACCESS_LIFETIME))

    def test_randomized_phase_spreads_past_half_a_lifetime(self) -> None:
        lifetime = 60.0
        times = [
            proactive_refresh_time(0.0, random.Random(100 + index), lifetime, True)
            for index in range(32)
        ]
        self.assertGreaterEqual(max(times) - min(times), 0.5 * lifetime)
        buckets: dict[int, int] = {}
        for tick in times:
            bucket = int(tick)
            buckets[bucket] = buckets.get(bucket, 0) + 1
        self.assertLessEqual(max(buckets.values()), 8)
        self.assertTrue(all(0.5 * lifetime <= tick <= 1.5 * lifetime for tick in times))

    def test_resume_after_outage_uses_the_same_draw(self) -> None:
        release = 5_000.0
        plain = [
            resume_after_outage(release, random.Random(index), ACCESS_LIFETIME, False)
            for index in range(32)
        ]
        self.assertEqual(plain, [release] * 32)
        spread = [
            resume_after_outage(release, random.Random(100 + index), ACCESS_LIFETIME, True)
            for index in range(32)
        ]
        self.assertTrue(all(tick != release for tick in spread))
        self.assertTrue(all(tick >= release + 0.5 * ACCESS_LIFETIME for tick in spread))
        self.assertGreaterEqual(max(spread) - min(spread), 0.5 * ACCESS_LIFETIME)


class ContentionTest(unittest.TestCase):
    def test_full_jitter_calls_the_slot_fewer_times(self) -> None:
        none = simulate_contention("none", seed=1)
        full = simulate_contention("full", seed=1)
        equal = simulate_contention("equal", seed=1)
        decorr = simulate_contention("decorr", seed=1)
        self.assertEqual(none["done"], 32)
        self.assertEqual(full["done"], 32)
        self.assertGreater(none["calls"], full["calls"])
        self.assertEqual(none["calls"], 528)
        self.assertGreater(equal["calls"], 0)
        self.assertGreater(decorr["calls"], 0)
        self.assertIn("finish_ms", equal)
        self.assertIn("finish_ms", decorr)
        self.assertGreater(equal["finish_ms"], 0)
        self.assertGreater(decorr["finish_ms"], 0)


if __name__ == "__main__":
    unittest.main()
