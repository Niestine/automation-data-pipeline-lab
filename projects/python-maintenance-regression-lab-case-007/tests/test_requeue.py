"""The work-list desk honors the same cap and delay the loop desk honors."""

from __future__ import annotations

import unittest

import helpers
from bay_notice.errors import OracleFailure, TransientGateError
from bay_notice.inject import Step
from bay_notice.policy import assert_cap, assert_positive_delays


def script() -> list[Step]:
    return [
        Step(kind="error", error_name="transient", partial="HOLD-"),
        Step(kind="success", body="HOLD C-14 40min", rtt_s=0.25),
    ]


class RequeueTests(unittest.TestCase):
    def test_requeue_matches_the_loop_oracles(self) -> None:
        loop = helpers.make_desk(steps=script(), floor=0.5, ceiling=4)
        queued = helpers.make_desk(steps=script(), floor=0.5, ceiling=4)
        loop_result = loop.run(helpers.sample_job())
        queued_result = queued.run_requeue(helpers.sample_job())
        self.assertEqual(loop_result.transmissions, queued_result.transmissions)
        self.assertEqual(loop_result.body, queued_result.body)
        self.assertEqual(loop_result.decisions, queued_result.decisions)
        self.assertEqual(loop.clock.waits, queued.clock.waits)
        self.assertEqual(queued.clock.waits, [0.5])
        self.assertEqual(queued_result.commits, 1)
        assert_positive_delays(queued.clock.waits)

    def test_requeue_cap_defect_continues_past_the_loop_ceiling(self) -> None:
        loop = helpers.make_desk(steps=helpers.errors(8), max_retries=3)
        queued = helpers.make_desk(build="requeue-drops-cap", steps=helpers.errors(8), max_retries=3)
        with self.assertRaises(TransientGateError):
            loop.run(helpers.sample_job())
        with self.assertRaises(TransientGateError):
            queued.run_requeue(helpers.sample_job())
        self.assertEqual(loop.transmissions, 4)
        assert_cap(loop.transmissions, 3)
        self.assertEqual(queued.transmissions, 5)
        with self.assertRaises(OracleFailure):
            assert_cap(queued.transmissions, 3)

    def test_requeue_delay_defect_records_a_zero_gap(self) -> None:
        queued = helpers.make_desk(
            build="requeue-drops-delay",
            steps=helpers.errors(6),
            max_retries=3,
        )
        with self.assertRaises(TransientGateError):
            queued.run_requeue(helpers.sample_job())
        self.assertEqual(queued.clock.waits, [0.0, 0.0, 0.0])
        with self.assertRaises(OracleFailure):
            assert_positive_delays(queued.clock.waits)

        loop = helpers.make_desk(steps=helpers.errors(6), max_retries=3, floor=1, ceiling=8)
        with self.assertRaises(TransientGateError):
            loop.run(helpers.sample_job())
        self.assertEqual(loop.clock.waits, [1.0, 2.0, 4.0])
        assert_positive_delays(loop.clock.waits)


if __name__ == "__main__":
    unittest.main()
