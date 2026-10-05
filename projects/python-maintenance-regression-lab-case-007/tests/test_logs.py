"""Structured attempt records. A wording change must not be the oracle."""

from __future__ import annotations

import unittest

import helpers
from bay_notice.inject import Step

FIELDS = ("operation_id", "attempt", "token", "error_type", "decision", "delay_s", "rto_s")


class LogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs = helpers.capture_logs()

    def tearDown(self) -> None:
        helpers.release_logs()

    def _run(self, template: str) -> None:
        desk = helpers.make_desk(
            steps=[
                Step(kind="error", error_name="transient"),
                Step(kind="success", body="HOLD C-14 40min", rtt_s=0.25),
            ],
            floor=0.5,
            ceiling=4,
            template=template,
        )
        desk.run(helpers.sample_job())

    def test_each_attempt_carries_the_record_fields(self) -> None:
        self._run("bay {operation_id} attempt {attempt} decision {decision}")
        self.assertEqual(len(self.logs.records), 2)
        first, second = self.logs.records
        for record in (first, second):
            for name in FIELDS:
                self.assertTrue(hasattr(record, name), name)
        self.assertEqual(first.operation_id, "BAY-1001")
        self.assertEqual(first.attempt, 0)
        self.assertEqual(first.token, "BAY-1001.0")
        self.assertEqual(first.error_type, "TransientGateError")
        self.assertEqual(first.decision, "retry")
        self.assertEqual(first.delay_s, 0.5)
        self.assertEqual(first.rto_s, 0.5)
        self.assertEqual(second.attempt, 1)
        self.assertEqual(second.token, "BAY-1001.1")
        self.assertEqual(second.error_type, "")
        self.assertEqual(second.decision, "stop")
        self.assertEqual(second.delay_s, 0.0)
        self.assertIn("decision retry", first.getMessage())

    def test_sentence_search_breaks_when_the_wording_changes(self) -> None:
        self._run("note {operation_id}/{attempt}")
        first = self.logs.records[0]
        self.assertNotIn("decision", first.getMessage())
        self.assertEqual(first.decision, "retry")
        self.assertEqual(first.operation_id, "BAY-1001")
        self.assertEqual(first.delay_s, 0.5)


if __name__ == "__main__":
    unittest.main()
