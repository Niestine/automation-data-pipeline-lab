import tempfile
import unittest

import helpers  # noqa: F401
from helpers import catalog_dict, job_dict, make_runner

from automation_job_lab.errors import SimulatedCrash
from automation_job_lab.models import LAB_EPOCH_MS
from automation_job_lab.seed import WINDOW_MS, build_inbox


class RunnerTests(unittest.TestCase):
    def test_happy_path_without_faults(self):
        runner, workspace, sleeper, logger, _, _, _ = make_runner(faults=[])
        report = runner.run()
        self.assertEqual(report.status, "complete")
        self.assertEqual([item.job_id for item in report.jobs], [
            "heartbeat-log",
            "ingest-inbox",
            "transform-records",
            "export-report",
            "cleanup-inbox",
            "notify-ops",
        ])
        self.assertTrue(all(item.status == "succeeded" for item in report.jobs))
        self.assertEqual(workspace.counts()["clean"], 10)
        self.assertEqual(workspace.counts()["dead_letter"], 2)
        self.assertEqual(workspace.counts()["archive"], 10)
        self.assertEqual(workspace.counts()["inbox"], 2)
        self.assertEqual(workspace.counts()["exports"], 1)
        self.assertEqual(workspace.counts()["notifications"], 1)
        self.assertEqual(workspace.counts()["heartbeats"], 1)
        self.assertEqual(sleeper.delays, [])
        self.assertTrue(logger.of_type("pipeline_complete"))

    def test_default_faults_retry_then_succeed(self):
        runner, workspace, sleeper, logger, _, _, _ = make_runner(
            faults=[
                {"job_id": "ingest-inbox", "attempt": 1, "error": "timeout"},
                {"job_id": "transform-records", "attempt": 1, "error": "transient_io"},
            ]
        )
        report = runner.run()
        self.assertEqual(report.status, "complete")
        self.assertEqual(report.retries, 2)
        self.assertEqual(report.job_map()["ingest-inbox"].attempts, 2)
        self.assertEqual(report.job_map()["transform-records"].attempts, 2)
        self.assertEqual(len(sleeper.delays), 2)
        self.assertEqual(workspace.counts()["clean"], 10)

    def test_dependency_skip_when_ingest_fails(self):
        runner, workspace, _, _, _, _, _ = make_runner(
            faults=[{"job_id": "ingest-inbox", "attempt": 1, "error": "validation"}]
        )
        report = runner.run()
        jobs = report.job_map()
        self.assertEqual(report.status, "failed")
        self.assertEqual(jobs["heartbeat-log"].status, "succeeded")
        self.assertEqual(jobs["ingest-inbox"].status, "failed")
        self.assertEqual(jobs["transform-records"].status, "skipped")
        self.assertEqual(jobs["export-report"].status, "skipped")
        self.assertEqual(jobs["cleanup-inbox"].status, "skipped")
        self.assertEqual(jobs["notify-ops"].status, "skipped")
        self.assertEqual(workspace.counts()["staging"], 0)

    def test_fail_fast_stops_before_later_independent_work(self):
        catalog = catalog_dict(
            job_dict(id="job-a", handler="heartbeat"),
            job_dict(id="job-b", handler="heartbeat"),
        )
        runner, _, _, _, _, _, _ = make_runner(
            catalog=catalog,
            inbox=[],
            faults=[{"job_id": "job-a", "error": "validation"}],
            fail_fast=True,
        )
        report = runner.run()
        self.assertEqual([item.job_id for item in report.jobs], ["job-a"])
        self.assertEqual(report.status, "failed")

    def test_cron_skips_heartbeat_off_the_minute(self):
        runner, _, _, _, _, _, _ = make_runner(
            faults=[],
            now_ms=LAB_EPOCH_MS + 13 * 60_000,
        )
        report = runner.run()
        self.assertEqual(report.job_map()["heartbeat-log"].status, "skipped")
        self.assertEqual(report.job_map()["heartbeat-log"].reason, "not_due")
        self.assertEqual(report.job_map()["ingest-inbox"].status, "succeeded")
        self.assertEqual(report.status, "complete")

    def test_force_runs_cron_job_off_the_minute(self):
        runner, _, _, _, _, _, _ = make_runner(
            faults=[],
            now_ms=LAB_EPOCH_MS + 13 * 60_000,
        )
        report = runner.run(force=True)
        self.assertEqual(report.job_map()["heartbeat-log"].status, "succeeded")

    def test_dry_run_overlay_and_no_checkpoint(self):
        runner, workspace, _, _, _, ledger, _ = make_runner(faults=[])
        report = runner.run(dry_run=True)
        self.assertEqual(report.status, "complete")
        self.assertTrue(report.dry_run)
        self.assertEqual(len(workspace.list("staging")), 12)
        self.assertEqual(workspace.records["staging"], {})
        self.assertEqual(workspace.records["clean"], {})
        self.assertEqual(len(ledger), 0)
        self.assertEqual(runner.store.load(report.run_id), None)

    def test_crash_after_handler_resumes_from_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, workspace, _, _, _, ledger, leases = make_runner(
                faults=[],
                crash_after_jobs=2,
                crash_at="post_handler",
                state_dir=tmp,
            )
            with self.assertRaises(SimulatedCrash):
                runner.run()
            self.assertEqual(workspace.counts()["staging"], 12)
            self.assertTrue(ledger.for_job("ingest-inbox"))
            held = leases.get("ingest-inbox")
            self.assertIsNotNone(held)
            runner.crash_after_jobs = None
            report = runner.run()
            self.assertTrue(report.resumed)
            self.assertEqual(report.status, "complete")
            ingest = report.job_map()["ingest-inbox"]
            self.assertEqual(ingest.status, "replayed")
            self.assertEqual(workspace.counts()["archive"], 10)
            self.assertIsNone(leases.get("ingest-inbox"))

    def test_crash_records_the_interrupted_job_in_the_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _, _, logger, _, _, _ = make_runner(
                faults=[],
                crash_after_jobs=2,
                crash_at="post_handler",
                state_dir=tmp,
            )
            with self.assertRaises(SimulatedCrash):
                runner.run()
            run_id = f"hourly-ops:w{LAB_EPOCH_MS}"
            saved = runner.store.load(run_id)
            self.assertEqual(saved.current_job, "ingest-inbox")
            self.assertEqual(saved.completed_jobs, ["heartbeat-log"])
            self.assertEqual(logger.of_type("pipeline_crash")[0]["current_job"], "ingest-inbox")
            runner.crash_after_jobs = None
            runner.run()
            self.assertEqual(logger.of_type("pipeline_start")[-1]["interrupted_job"], "ingest-inbox")
            self.assertIsNone(runner.store.load(run_id).current_job)

    def test_crash_after_lease_release_resumes_at_the_next_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, workspace, _, _, _, _, leases = make_runner(
                faults=[],
                crash_after_jobs=3,
                crash_at="post_lease_release",
                state_dir=tmp,
            )
            with self.assertRaises(SimulatedCrash):
                runner.run()
            self.assertIsNone(leases.get("transform-records"))
            self.assertEqual(workspace.counts()["clean"], 10)
            runner.crash_after_jobs = None
            report = runner.run()
            jobs = report.job_map()
            self.assertEqual(jobs["transform-records"].status, "already_complete")
            self.assertEqual(jobs["export-report"].status, "succeeded")
            self.assertEqual(report.status, "complete")
            self.assertEqual(workspace.counts()["archive"], 10)

    def test_structured_events_carry_clock_timestamps(self):
        runner, _, _, logger, clock, _, _ = make_runner(
            faults=[{"job_id": "ingest-inbox", "attempt": 1, "error": "timeout"}]
        )
        runner.run()
        retry = logger.of_type("job_retry")[0]
        self.assertEqual(retry["ts_ms"], LAB_EPOCH_MS)
        second_start = [item for item in logger.of_type("job_start") if item["job_id"] == "ingest-inbox"][1]
        self.assertGreater(second_start["ts_ms"], LAB_EPOCH_MS)
        self.assertTrue(all("ts_ms" in item for item in logger.events))

    def test_crash_after_checkpoint_skips_completed_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, workspace, _, _, _, _, _ = make_runner(
                faults=[],
                crash_after_jobs=2,
                crash_at="post_checkpoint",
                state_dir=tmp,
            )
            with self.assertRaises(SimulatedCrash):
                runner.run()
            runner.crash_after_jobs = None
            report = runner.run()
            self.assertEqual(report.job_map()["heartbeat-log"].status, "already_complete")
            self.assertEqual(report.job_map()["ingest-inbox"].status, "already_complete")
            self.assertEqual(report.job_map()["transform-records"].status, "succeeded")
            self.assertEqual(report.status, "complete")
            self.assertEqual(workspace.counts()["clean"], 10)

    def test_second_window_picks_up_new_inbox_rows(self):
        runner, workspace, _, _, clock, _, _ = make_runner(faults=[])
        first = runner.run()
        self.assertEqual(first.status, "complete")
        for row in build_inbox(window=1):
            workspace.upsert("inbox", row)
        clock.advance_ms(WINDOW_MS)
        second = runner.run()
        self.assertEqual(second.status, "complete")
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(workspace.counts()["clean"], 13)
        self.assertEqual(workspace.counts()["archive"], 13)
        self.assertEqual(workspace.counts()["exports"], 2)
        self.assertEqual(workspace.counts()["heartbeats"], 2)
        self.assertEqual(workspace.counts()["inbox"], 2)
