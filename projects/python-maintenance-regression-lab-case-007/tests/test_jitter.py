"""Fixed jitter stacks clients. A seeded sample spreads them without dropping the floor."""

from __future__ import annotations

import unittest

import helpers
from bay_notice.errors import OracleFailure, TransientGateError
from bay_notice.policy import SequenceSampler, apply_jitter, assert_desynchronized


class JitterTests(unittest.TestCase):
    def _sequences(self, jitter: str) -> list[list[float]]:
        sequences = []
        for seed in range(1, 5):
            desk = helpers.make_desk(
                steps=helpers.errors(8),
                max_retries=4,
                floor=1,
                ceiling=6,
                jitter=jitter,
                seed=seed,
                spread=0.3,
            )
            with self.assertRaises(TransientGateError):
                desk.run(helpers.sample_job(f"BAY-100{seed}"))
            sequences.append(list(desk.clock.waits))
            self.assertGreaterEqual(min(desk.clock.waits), 1.0)
        return sequences

    def test_fixed_sampler_is_the_synchronized_antipattern(self) -> None:
        sequences = self._sequences("fixed")
        self.assertEqual(len({tuple(item) for item in sequences}), 1)
        self.assertEqual(sequences[0], [1.0, 2.0, 4.0, 6.0])
        with self.assertRaises(OracleFailure):
            assert_desynchronized(sequences)

    def test_seeded_sampler_desynchronizes_without_breaking_the_floor(self) -> None:
        sequences = self._sequences("seeded")
        self.assertGreater(len({tuple(item) for item in sequences}), 1)
        assert_desynchronized(sequences)

    def test_jitter_cannot_pull_a_positive_floor_down(self) -> None:
        self.assertEqual(apply_jitter(1.0, 1.0, 6.0, 0.1), 1.0)
        self.assertLess(apply_jitter(2.0, 0.0, 10.0, 0.5), 2.0)
        desk = helpers.make_desk(
            steps=helpers.errors(4),
            max_retries=2,
            floor=1,
            ceiling=6,
            sampler=SequenceSampler([0.1, 0.1]),
        )
        with self.assertRaises(TransientGateError):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.clock.waits, [1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
