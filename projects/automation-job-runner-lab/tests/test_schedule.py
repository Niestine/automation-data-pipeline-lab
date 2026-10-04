import unittest

import helpers  # noqa: F401
from helpers import catalog_dict, job_dict, make_runner

from automation_job_lab.errors import SchemaError
from automation_job_lab.models import LAB_EPOCH_MS, ScheduleSpec
from automation_job_lab.schedule import cron_due, cron_matches, interval_due, window_start
from automation_job_lab.schema import load_catalog

HOUR = 3_600_000


class ScheduleTests(unittest.TestCase):
    def test_window_start_aligns_to_epoch(self):
        self.assertEqual(window_start(LAB_EPOCH_MS, HOUR), LAB_EPOCH_MS)
        self.assertEqual(window_start(LAB_EPOCH_MS + 13 * 60_000, HOUR), LAB_EPOCH_MS)
        self.assertEqual(window_start(LAB_EPOCH_MS + HOUR, HOUR), LAB_EPOCH_MS + HOUR)

    def test_offset_shifts_the_grid(self):
        self.assertEqual(window_start(1_500, 1_000, offset_ms=500), 1_500)

    def test_interval_due_every_other_window(self):
        spec = ScheduleSpec(every_ms=2 * HOUR)
        self.assertTrue(interval_due(spec, LAB_EPOCH_MS))
        self.assertFalse(interval_due(spec, LAB_EPOCH_MS + HOUR))
        self.assertTrue(interval_due(spec, LAB_EPOCH_MS + 2 * HOUR))

    def test_interval_offset_moves_the_due_window(self):
        spec = ScheduleSpec(every_ms=2 * HOUR, offset_ms=HOUR)
        self.assertFalse(interval_due(spec, LAB_EPOCH_MS))
        self.assertTrue(interval_due(spec, LAB_EPOCH_MS + HOUR))

    def test_no_interval_is_always_due(self):
        self.assertTrue(interval_due(ScheduleSpec(), LAB_EPOCH_MS + HOUR))

    def test_cron_matches_minute_zero_at_epoch(self):
        spec = ScheduleSpec(cron_minute=0)
        self.assertTrue(cron_matches(spec, LAB_EPOCH_MS))
        self.assertFalse(cron_matches(spec, LAB_EPOCH_MS + 13 * 60_000))

    def test_cron_hour_gate(self):
        spec = ScheduleSpec(cron_minute=0, cron_hour=1)
        self.assertFalse(cron_matches(spec, LAB_EPOCH_MS))
        self.assertTrue(cron_matches(spec, LAB_EPOCH_MS + HOUR))

    def test_cron_not_due_twice_in_the_same_minute(self):
        spec = ScheduleSpec(cron_minute=0)
        self.assertTrue(cron_due(spec, None, LAB_EPOCH_MS))
        self.assertFalse(cron_due(spec, LAB_EPOCH_MS, LAB_EPOCH_MS + 1_000))
        self.assertTrue(cron_due(spec, LAB_EPOCH_MS, LAB_EPOCH_MS + HOUR))

    def test_runner_skips_interval_job_in_off_windows(self):
        catalog = catalog_dict(
            job_dict(id="every-hour"),
            job_dict(id="every-two-hours", schedule={"every_ms": 2 * HOUR}),
            job_dict(id="after-two-hours", depends_on=["every-two-hours"]),
        )
        runner, workspace, _, _, clock, _, _ = make_runner(catalog=catalog, inbox=[], faults=[])
        first = runner.run().job_map()
        self.assertEqual(first["every-two-hours"].status, "succeeded")
        self.assertEqual(first["after-two-hours"].status, "succeeded")
        clock.advance_ms(HOUR)
        second = runner.run()
        jobs = second.job_map()
        self.assertEqual(second.status, "complete")
        self.assertEqual(jobs["every-hour"].status, "succeeded")
        self.assertEqual(jobs["every-two-hours"].reason, "not_due")
        self.assertEqual(jobs["after-two-hours"].reason, "dependency_skipped")
        clock.advance_ms(HOUR)
        third = runner.run().job_map()
        self.assertEqual(third["every-two-hours"].status, "succeeded")
        self.assertEqual(len(workspace.list("heartbeats")), 3)

    def test_interval_must_align_with_the_pipeline_window(self):
        bad = [
            {"every_ms": HOUR + 1_000},
            {"every_ms": 2 * HOUR, "offset_ms": 2 * HOUR},
            {"every_ms": 2 * HOUR, "offset_ms": 1_000},
            {"offset_ms": HOUR},
        ]
        for schedule in bad:
            with self.subTest(schedule=schedule):
                with self.assertRaises(SchemaError):
                    load_catalog(catalog_dict(job_dict(schedule=schedule)))

