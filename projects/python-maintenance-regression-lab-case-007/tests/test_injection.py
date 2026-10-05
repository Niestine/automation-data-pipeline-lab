"""Fault injection, cap oracle, delay oracle, predicate, and buffer reset."""

from __future__ import annotations

import json
import unittest

import helpers
from bay_notice.errors import (
    JobRejected,
    OracleFailure,
    PermanentGateError,
    TransientGateError,
)
from bay_notice.inject import Job, Step, job_from_dict, step_from_dict
from bay_notice.policy import assert_cap, assert_positive_delays


class InjectionTests(unittest.TestCase):
    def test_success_path_stays_single_call_until_a_fault_is_injected(self) -> None:
        quiet = helpers.make_desk(steps=[])
        result = quiet.run(helpers.sample_job())
        self.assertTrue(result.ok)
        self.assertEqual(result.transmissions, 1)
        self.assertEqual(quiet.gateway.sends, 1)

        forced = helpers.make_desk(
            steps=[
                Step(kind="error", error_name="transient"),
                Step(kind="success", body="HOLD C-14 40min", rtt_s=0.25),
            ]
        )
        repaired = forced.run(helpers.sample_job())
        self.assertTrue(repaired.ok)
        self.assertEqual(repaired.transmissions, 2)
        self.assertEqual(repaired.body, "HOLD C-14 40min")
        self.assertEqual(repaired.decisions, ["retry", "stop"])

    def test_cap_oracle_flags_the_uncapped_build(self) -> None:
        desk = helpers.make_desk(build="ignore-cap", steps=helpers.errors(8), max_retries=3)
        with self.assertRaises(TransientGateError):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.transmissions, 5)
        with self.assertRaises(OracleFailure):
            assert_cap(desk.transmissions, 3)

    def test_cap_oracle_passes_on_the_repair(self) -> None:
        desk = helpers.make_desk(build="repaired", steps=helpers.errors(8), max_retries=3)
        with self.assertRaises(TransientGateError):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.transmissions, 4)
        assert_cap(desk.transmissions, 3)
        self.assertEqual(desk.decisions[-1], "stop")

    def test_delay_oracle_flags_zero_gaps(self) -> None:
        desk = helpers.make_desk(build="no-backoff", steps=helpers.errors(6), max_retries=3)
        with self.assertRaises(TransientGateError):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.transmissions, 4)
        self.assertEqual(desk.clock.waits, [0.0, 0.0, 0.0])
        with self.assertRaises(OracleFailure):
            assert_positive_delays(desk.clock.waits)

    def test_repaired_delays_follow_the_rto_table(self) -> None:
        desk = helpers.make_desk(
            steps=helpers.errors(8),
            max_retries=4,
            floor=1.0,
            ceiling=6.0,
        )
        with self.assertRaises(TransientGateError):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.clock.waits, [1.0, 2.0, 4.0, 6.0])
        assert_positive_delays(desk.clock.waits)
        self.assertEqual(desk.transmissions, 5)

    def test_permanent_error_is_not_retried_and_keeps_its_type(self) -> None:
        repair = helpers.make_desk(steps=[Step(kind="error", error_name="permanent")])
        with self.assertRaises(PermanentGateError) as caught:
            repair.run(helpers.sample_job())
        self.assertIs(type(caught.exception), PermanentGateError)
        self.assertEqual(repair.transmissions, 1)
        self.assertEqual(repair.decisions, ["not_retryable"])

        broken = helpers.make_desk(
            build="retry-permanent",
            steps=helpers.errors(8, name="permanent"),
            max_retries=3,
        )
        with self.assertRaises(PermanentGateError):
            broken.run(helpers.sample_job())
        self.assertEqual(broken.transmissions, 4)

    def test_skip_transient_build_does_not_retry_a_declared_transient(self) -> None:
        broken = helpers.make_desk(
            build="skip-transient",
            steps=[
                Step(kind="error", error_name="transient"),
                Step(kind="success", body="HOLD C-14 40min"),
            ],
        )
        with self.assertRaises(TransientGateError):
            broken.run(helpers.sample_job())
        self.assertEqual(broken.transmissions, 1)

    def test_subclass_outside_the_predicate_is_not_retried(self) -> None:
        class Extra(TransientGateError):
            pass

        desk = helpers.make_desk(steps=[Step(kind="error", error=Extra("no"))])
        with self.assertRaises(Extra):
            desk.run(helpers.sample_job())
        self.assertEqual(desk.transmissions, 1)

    def test_partial_attempt_text_is_absent_from_the_successful_body(self) -> None:
        steps = [
            Step(kind="error", error_name="transient", partial="HOLD-"),
            Step(kind="success", body="HOLD C-14 40min"),
        ]
        repair = helpers.make_desk(steps=list(steps))
        result = repair.run(helpers.sample_job())
        self.assertEqual(result.body, "HOLD C-14 40min")

        broken = helpers.make_desk(build="concat-partial", steps=list(steps))
        leaked = broken.run(helpers.sample_job())
        self.assertEqual(leaked.body, "HOLD-HOLD C-14 40min")

    def test_invalid_notice_is_rejected_before_a_send(self) -> None:
        desk = helpers.make_desk(steps=[])
        with self.assertRaises(JobRejected):
            desk.run(Job("BAY-1001", "C-14", "HOLD C-14 40min", "18"))
        self.assertEqual(desk.transmissions, 0)
        with self.assertRaises(JobRejected):
            desk.run(Job("bay-1001", "C-14", "HOLD C-14 40min", "18.00"))
        with self.assertRaises(JobRejected):
            desk.run(Job("BAY-1001", "c-14", "HOLD C-14 40min", "18.00"))

    def test_charge_needs_ascii_digits(self) -> None:
        desk = helpers.make_desk(steps=[])
        with self.assertRaises(JobRejected):
            desk.run(Job("BAY-1001", "C-14", "HOLD C-14 40min", "\u0661\u0668.00"))
        with self.assertRaises(JobRejected):
            desk.run(Job("BAY-1001", "C-14", "HOLD C-14 40min", "-1.00"))
        self.assertEqual(desk.transmissions, 0)

    def test_a_reused_desk_reports_each_notice_on_its_own(self) -> None:
        desk = helpers.make_desk(steps=[Step(kind="error", error_name="transient")])
        first = desk.run(helpers.sample_job("BAY-1001"))
        second = desk.run(helpers.sample_job("BAY-1002"))
        self.assertEqual(first.transmissions, 2)
        self.assertEqual(second.transmissions, 1)
        self.assertEqual(second.decisions, ["stop"])
        self.assertEqual(second.commits, 2)
        self.assertEqual(desk.gateway.sends, 3)

    def test_unknown_gate_error_name_is_not_a_job_rejection(self) -> None:
        with self.assertRaises(ValueError) as caught:
            step_from_dict({"kind": "error", "error_name": "melted"})
        self.assertNotIsInstance(caught.exception, JobRejected)

    def test_example_jobs_validate(self) -> None:
        raw = json.loads((helpers.EXAMPLES / "jobs.json").read_text(encoding="utf-8"))
        jobs = [job_from_dict(item) for item in raw["jobs"]]
        self.assertEqual([job.operation_id for job in jobs], ["BAY-1001", "BAY-1002", "BAY-1003"])
        self.assertEqual(jobs[2].charge, "0.00")


if __name__ == "__main__":
    unittest.main()
