"""Karn token rule and the operation-id ledger."""

from __future__ import annotations

import unittest

import helpers
from bay_notice.errors import TransientGateError
from bay_notice.inject import Step
from bay_notice.ledger import Ledger


def karn_steps() -> list[Step]:
    return [
        Step(kind="timeout"),
        Step(kind="late", token="BAY-1001.0", body="HOLD C-14 40min", rtt_s=4.0),
        Step(kind="success", body="HOLD C-14 40min", rtt_s=1.0),
    ]


class KarnTests(unittest.TestCase):
    def test_stale_token_does_not_sample_or_commit(self) -> None:
        desk = helpers.make_desk(steps=karn_steps(), floor=1, ceiling=60, granularity=0.05)
        result = desk.run(helpers.sample_job())
        self.assertEqual(len(desk.events), 1)
        stale = desk.events[0]
        self.assertEqual(stale["outstanding"], "BAY-1001.1")
        self.assertEqual(stale["got"], "BAY-1001.0")
        self.assertEqual(stale["commits"], 0)
        self.assertIsNone(stale["srtt"])
        self.assertEqual(result.commits, 1)
        self.assertEqual(desk.timer.srtt, 1.0)
        self.assertEqual(desk.timer.rttvar, 0.5)
        self.assertEqual(desk.ledger.charge_cents_total, 1800)
        self.assertEqual(desk.transmissions, 2)

        before = desk.timer.srtt
        self.assertFalse(desk.deliver(Step(kind="success", token="BAY-1001.1", body="HOLD C-14 40min")))
        self.assertEqual(result.commits, 1)
        self.assertEqual(len(desk.ledger.entries), 1)
        self.assertEqual(desk.timer.srtt, before)
        self.assertEqual(desk.ledger.charge_cents_total, 1800)

    def test_accepting_the_stale_token_charges_twice_and_moves_srtt(self) -> None:
        desk = helpers.make_desk(
            build="accept-stale",
            steps=karn_steps(),
            floor=1,
            ceiling=60,
            granularity=0.05,
        )
        desk.run(helpers.sample_job())
        stale = desk.events[0]
        self.assertEqual(stale["outstanding"], "BAY-1001.1")
        self.assertEqual(stale["commits"], 1)
        self.assertEqual(stale["srtt"], 4.0)
        self.assertEqual(len(desk.ledger.entries), 2)
        self.assertEqual(desk.timer.srtt, 3.625)
        self.assertEqual(desk.timer.rttvar, 2.25)
        self.assertEqual(desk.ledger.charge_cents_total, 3600)
        self.assertFalse(
            desk.deliver(Step(kind="success", token="BAY-1001.1", body="HOLD C-14 40min"))
        )
        self.assertEqual(len(desk.ledger.entries), 2)

    def test_late_delivery_after_the_desk_gave_up_does_not_charge(self) -> None:
        desk = helpers.make_desk(steps=helpers.errors(2), max_retries=1)
        with self.assertRaises(TransientGateError):
            desk.run(helpers.sample_job())
        self.assertFalse(desk.deliver(Step(kind="success", body="HOLD C-14 40min")))
        self.assertFalse(
            desk.deliver(Step(kind="success", token="BAY-1001.1", body="HOLD C-14 40min"))
        )
        self.assertEqual(desk.ledger.entries, [])
        self.assertIsNone(desk.timer.srtt)

    def test_ledger_operation_id_ignores_a_second_body(self) -> None:
        ledger = Ledger()
        self.assertTrue(ledger.commit("BAY-1001", "BAY-1001.0", "HOLD C-14 40min", "18.00"))
        self.assertFalse(ledger.commit("BAY-1001", "BAY-1001.1", "HOLD C-14 again", "18.00"))
        self.assertEqual(len(ledger.entries), 1)
        self.assertEqual(ledger.entries[0].body, "HOLD C-14 40min")
        self.assertTrue(ledger.commit("BAY-1002", "BAY-1002.0", "HOLD C-15 20min", "9.00"))
        self.assertEqual(ledger.charge_cents_total, 2700)


if __name__ == "__main__":
    unittest.main()
