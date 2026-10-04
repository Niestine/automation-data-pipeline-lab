import unittest
from dataclasses import replace

import helpers  # noqa: F401
from helpers import catalog_dict, job_dict, make_runner

from automation_job_lab.runner import make_fingerprint, make_key
from automation_job_lab.schema import load_catalog
from automation_job_lab.seed import WINDOW_MS, build_inbox


class IdempotencyTests(unittest.TestCase):
    def test_second_run_in_the_same_window_replays(self):
        runner, workspace, _, logger, _, ledger, _ = make_runner(faults=[])
        first = runner.run()
        self.assertEqual(first.status, "complete")
        staging_after_first = workspace.counts()["staging"]
        second = runner.run()
        self.assertTrue(second.resumed)
        self.assertEqual(second.status, "complete")
        self.assertTrue(all(item.status == "already_complete" for item in second.jobs))
        self.assertEqual(workspace.counts()["archive"], 10)
        self.assertEqual(workspace.counts()["staging"], staging_after_first)
        self.assertEqual(logger.of_type("pipeline_already_complete")[-1]["run_id"], first.run_id)
        self.assertGreater(len(ledger), 0)

    def test_force_replays_from_the_ledger_without_duplicating_archive(self):
        runner, workspace, _, logger, _, _, _ = make_runner(faults=[])
        runner.run()
        report = runner.run(force=True)
        self.assertEqual(report.status, "complete")
        self.assertTrue(all(item.status == "replayed" for item in report.jobs))
        self.assertEqual(workspace.counts()["archive"], 10)
        self.assertEqual(workspace.counts()["inbox"], 2)
        self.assertEqual(len(logger.of_type("job_replayed")), 6)

    def test_failed_execution_is_not_replayed_as_success(self):
        catalog = catalog_dict(job_dict(id="heartbeat-log"))
        runner, workspace, _, _, _, ledger, _ = make_runner(
            catalog=catalog,
            inbox=[],
            faults=[{"job_id": "heartbeat-log", "attempt": 1, "error": "validation"}],
        )
        failed = runner.run()
        self.assertEqual(failed.job_map()["heartbeat-log"].status, "failed")
        records = ledger.for_job("heartbeat-log")
        self.assertEqual(records[0].status, "failed")
        self.assertEqual(failed.status, "failed")
        # The one-shot fault is consumed; rerunning the failed window retries the same key.
        retry = runner.run()
        self.assertTrue(retry.resumed)
        self.assertEqual(retry.job_map()["heartbeat-log"].status, "succeeded")
        self.assertEqual(retry.status, "complete")
        self.assertEqual(ledger.for_job("heartbeat-log")[0].status, "succeeded")
        self.assertEqual(len(workspace.list("heartbeats")), 1)

    def test_fingerprint_mismatch_is_a_conflict(self):
        catalog = load_catalog(
            catalog_dict(
                job_dict(
                    id="ingest-inbox",
                    handler="ingest",
                    params={"source": "inbox", "dest": "staging"},
                    idempotency={"mode": "window"},
                )
            )
        )
        runner, _, _, _, _, _, _ = make_runner(catalog=catalog, faults=[])
        first = runner.run()
        self.assertEqual(first.job_map()["ingest-inbox"].status, "succeeded")
        mutated = replace(
            catalog.job("ingest-inbox"),
            params={"source": "inbox", "dest": "clean"},
        )
        runner.catalog = replace(catalog, jobs=(mutated,))
        runner.order = [mutated]
        conflict = runner.run(force=True)
        self.assertEqual(conflict.job_map()["ingest-inbox"].status, "failed")
        self.assertEqual(conflict.job_map()["ingest-inbox"].reason, "idempotency_conflict")

    def test_input_hash_changes_when_staging_grows(self):
        runner, workspace, _, _, _, _, _ = make_runner(faults=[])
        runner.run()
        transform = runner.catalog.job("transform-records")
        # input_hash keys ignore run/window; only the source bucket matters.
        key_before = make_key("hourly-ops", transform, "run-a", 0, workspace)
        self.assertEqual(key_before, make_key("hourly-ops", transform, "run-b", 99, workspace))
        workspace.upsert("staging", build_inbox(window=1)[0])
        key_after = make_key("hourly-ops", transform, "run-a", 0, workspace)
        self.assertNotEqual(key_before, key_after)
        self.assertTrue(key_after.startswith("hourly-ops:transform-records:h"))

    def test_updated_record_version_reprocesses_instead_of_conflicting(self):
        runner, workspace, _, _, clock, _, _ = make_runner(faults=[])
        self.assertEqual(runner.run().status, "complete")
        updated = dict(build_inbox(window=0)[0])
        updated["version"] = 2
        updated["updated_at"] = "2026-01-01T01:00:00Z"
        workspace.upsert("inbox", updated)
        clock.advance_ms(WINDOW_MS)
        report = runner.run()
        self.assertEqual(report.status, "complete")
        self.assertEqual(report.job_map()["transform-records"].status, "succeeded")
        self.assertEqual(workspace.get("clean", updated["id"])["version"], 2)
        self.assertEqual(workspace.get("archive", updated["id"])["version"], 2)

    def test_fingerprint_includes_source_versions_for_input_hash_jobs(self):
        runner, workspace, _, _, _, _, _ = make_runner(faults=[])
        for row in workspace.list("inbox"):
            workspace.upsert("staging", row)
        job = runner.catalog.job("transform-records")
        first = make_fingerprint(job, workspace)
        row = dict(workspace.list("staging")[0])
        row["version"] = int(row["version"]) + 1
        workspace.upsert("staging", row)
        second = make_fingerprint(job, workspace)
        self.assertNotEqual(first, second)
        ingest = runner.catalog.job("ingest-inbox")
        before = make_fingerprint(ingest, workspace)
        leftover = dict(workspace.list("inbox")[0])
        leftover["version"] = int(leftover["version"]) + 1
        workspace.upsert("inbox", leftover)
        after = make_fingerprint(ingest, workspace)
        self.assertEqual(before, after)
