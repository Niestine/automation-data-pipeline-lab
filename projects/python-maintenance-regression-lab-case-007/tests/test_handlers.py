"""Non-fatal handler fixtures and the FIXME/TODO scan."""

from __future__ import annotations

import unittest

import helpers
from bay_notice.errors import (
    OperationAborted,
    OracleFailure,
    TransientGateError,
    WrappedHandlerError,
)
from bay_notice.handlers import (
    broad_abort,
    dispatch_error,
    empty_handler,
    handler_markers,
    repair_handler,
    swallow_handler,
)
from bay_notice.inject import Step
from bay_notice.policy import assert_every_send_decided


class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs = helpers.capture_logs()

    def tearDown(self) -> None:
        helpers.release_logs()

    def test_empty_handler_reports_success_without_a_retry_decision(self) -> None:
        desk = helpers.make_desk(
            build="empty-handler",
            steps=[Step(kind="error", error_name="transient")],
        )
        result = desk.run(helpers.sample_job())
        self.assertTrue(result.ok)
        self.assertEqual(result.transmissions, 1)
        self.assertEqual(result.decisions, [])
        self.assertEqual(self.logs.records, [])

    def test_swallow_handler_logs_without_a_decision_field(self) -> None:
        desk = helpers.make_desk(
            build="swallow-handler",
            steps=[Step(kind="error", error_name="transient")],
        )
        result = desk.run(helpers.sample_job())
        self.assertTrue(result.ok)
        self.assertEqual(result.transmissions, 1)
        self.assertEqual(len(self.logs.records), 1)
        self.assertFalse(hasattr(self.logs.records[0], "decision"))
        self.assertIn("ignored", self.logs.records[0].getMessage())

    def test_rewrite_handler_raises_a_different_type(self) -> None:
        desk = helpers.make_desk(
            build="rewrite-handler",
            steps=[Step(kind="error", error_name="transient")],
        )
        with self.assertRaises(WrappedHandlerError) as caught:
            desk.run(helpers.sample_job())
        self.assertIsNot(type(caught.exception), TransientGateError)
        self.assertEqual(desk.transmissions, 1)

    def test_broad_abort_stops_a_declared_transient(self) -> None:
        desk = helpers.make_desk(
            build="broad-abort",
            steps=[
                Step(kind="error", error_name="transient"),
                Step(kind="success", body="HOLD C-14 40min"),
            ],
        )
        with self.assertRaises(OperationAborted) as caught:
            desk.run(helpers.sample_job())
        self.assertIsInstance(caught.exception.__cause__, TransientGateError)
        self.assertEqual(desk.transmissions, 1)

        repair = helpers.make_desk(
            steps=[
                Step(kind="error", error_name="transient"),
                Step(kind="success", body="HOLD C-14 40min"),
            ]
        )
        result = repair.run(helpers.sample_job())
        self.assertTrue(result.ok)
        self.assertEqual(result.transmissions, 2)
        self.assertEqual(result.decisions, ["retry", "stop"])

    def test_decision_oracle_rejects_the_silent_handlers_and_passes_the_repair(self) -> None:
        for build in ("empty-handler", "swallow-handler"):
            desk = helpers.make_desk(build=build, steps=[Step(kind="error", error_name="transient")])
            desk.run(helpers.sample_job())
            with self.assertRaises(OracleFailure, msg=build):
                assert_every_send_decided(desk.transmissions, desk.decisions)
        repair = helpers.make_desk(steps=[Step(kind="error", error_name="transient")])
        result = repair.run(helpers.sample_job())
        assert_every_send_decided(result.transmissions, result.decisions)

    def test_fixme_and_todo_markers_sit_only_on_the_broken_handlers(self) -> None:
        self.assertEqual(handler_markers(empty_handler), ["FIXME"])
        self.assertEqual(handler_markers(swallow_handler), ["TODO"])
        self.assertEqual(handler_markers(broad_abort), [])
        self.assertEqual(handler_markers(repair_handler), [])
        self.assertEqual(handler_markers(dispatch_error), [])


if __name__ == "__main__":
    unittest.main()
