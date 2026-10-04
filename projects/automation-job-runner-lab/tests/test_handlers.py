import unittest

import helpers  # noqa: F401
from helpers import catalog_dict, job_dict, make_runner

from automation_job_lab.errors import PermanentError
from automation_job_lab.handlers import (
    FaultInjector,
    HandlerContext,
    cleanup,
    ingest,
    notify,
    render_name,
    transform,
)
from automation_job_lab.schema import load_catalog
from automation_job_lab.seed import build_inbox
from automation_job_lab.store import Workspace
from automation_job_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper


def _ctx(job, workspace, *, dry_run=False, timeout_ms=5_000, sleep=None):
    clock = ManualClock()
    return HandlerContext(
        job=job,
        workspace=workspace,
        clock=clock,
        logger=JsonLogger(),
        sleeper=sleep or RecordingSleeper(clock),
        faults=FaultInjector(),
        dry_run=dry_run,
        attempt=1,
        run_id="hourly-ops:w1",
        window_start_ms=clock.now_ms(),
        deadline_ms=clock.now_ms() + timeout_ms,
        pipeline="hourly-ops",
    )


class HandlerTests(unittest.TestCase):
    def test_ingest_copies_every_row(self):
        catalog = load_catalog(
            catalog_dict(
                job_dict(
                    id="ingest-inbox",
                    handler="ingest",
                    params={"source": "inbox", "dest": "staging"},
                )
            )
        )
        workspace = Workspace()
        for row in build_inbox():
            workspace.upsert("inbox", row)
        result = ingest(_ctx(catalog.job("ingest-inbox"), workspace))
        self.assertEqual(result["inserted"], 12)
        self.assertEqual(len(workspace.list("staging")), 12)
        self.assertEqual(len(workspace.list("inbox")), 12)

    def test_transform_splits_clean_and_dead_letter(self):
        catalog = load_catalog(
            catalog_dict(
                job_dict(
                    id="ingest-inbox",
                    handler="ingest",
                    params={"source": "inbox", "dest": "staging"},
                ),
                job_dict(
                    id="transform-records",
                    handler="transform",
                    depends_on=["ingest-inbox"],
                    params={"source": "staging", "dest": "clean", "dead_letter": "dead_letter"},
                ),
            )
        )
        workspace = Workspace()
        for row in build_inbox():
            workspace.upsert("inbox", row)
        ingest(_ctx(catalog.job("ingest-inbox"), workspace))
        result = transform(_ctx(catalog.job("transform-records"), workspace))
        self.assertEqual(result["accepted"], 10)
        self.assertEqual(result["rejected"], 2)
        self.assertEqual(len(workspace.list("clean")), 10)
        self.assertEqual({row["id"] for row in workspace.list("dead_letter")}, {"REC-1098", "REC-1099"})

    def test_export_notify_cleanup_chain(self):
        runner, workspace, _, _, _, _, _ = make_runner(faults=[])
        report = runner.run()
        self.assertEqual(report.status, "complete")
        export_name = report.job_map()["export-report"].output["name"]
        self.assertTrue(export_name.startswith("report-"))
        blob = workspace.get("exports", export_name)
        self.assertEqual(blob["clean_count"], 10)
        note = workspace.list("notifications")[0]
        self.assertEqual(note["export_name"], export_name)
        self.assertEqual(note["channel"], "ops")
        self.assertEqual(workspace.counts()["archive"], 10)
        self.assertEqual(workspace.counts()["inbox"], 2)

    def test_dry_run_does_not_mutate_live_buckets(self):
        catalog = load_catalog(
            catalog_dict(
                job_dict(
                    id="ingest-inbox",
                    handler="ingest",
                    params={"source": "inbox", "dest": "staging"},
                )
            )
        )
        workspace = Workspace()
        for row in build_inbox():
            workspace.upsert("inbox", row)
        ingest(_ctx(catalog.job("ingest-inbox"), workspace, dry_run=True))
        self.assertEqual(workspace.records["staging"], {})
        self.assertEqual(len(workspace.list("staging")), 12)

    def test_deadline_fault_sleep_times_out(self):
        job = job_dict(
            id="heartbeat-log",
            timeout_ms=20,
            retry={
                "max_attempts": 1,
                "base_delay_ms": 10,
                "max_delay_ms": 10,
                "multiplier": 2.0,
                "jitter_ms": 0,
            },
        )
        runner, _, sleeper, _, _, _, _ = make_runner(
            catalog=catalog_dict(job),
            inbox=[],
            faults=[{"job_id": "heartbeat-log", "attempt": 1, "sleep_ms": 50}],
        )
        report = runner.run()
        self.assertEqual(report.job_map()["heartbeat-log"].status, "failed")
        self.assertEqual(report.job_map()["heartbeat-log"].reason, "timeout")
        self.assertEqual(sleeper.delays, [50])

    def test_render_name_rejects_parent_traversal(self):
        with self.assertRaises(PermanentError):
            render_name("../x.json", window=1)

    def test_cleanup_only_archives_clean_ids(self):
        catalog = load_catalog(
            catalog_dict(
                job_dict(
                    id="cleanup-inbox",
                    handler="cleanup",
                    params={"inbox": "inbox", "clean": "clean", "archive": "archive"},
                )
            )
        )
        workspace = Workspace()
        workspace.upsert("inbox", {"id": "REC-1001", "version": 1, "status": "pending"})
        workspace.upsert("inbox", {"id": "REC-1098", "version": 1, "status": "pending"})
        workspace.upsert("clean", {"id": "REC-1001", "version": 1, "status": "processed"})
        result = cleanup(_ctx(catalog.job("cleanup-inbox"), workspace))
        self.assertEqual(result["moved"], 1)
        self.assertEqual([row["id"] for row in workspace.list("inbox")], ["REC-1098"])
        self.assertEqual([row["id"] for row in workspace.list("archive")], ["REC-1001"])

    def test_notify_requires_export_blob(self):
        catalog = load_catalog(
            catalog_dict(
                job_dict(
                    id="notify-ops",
                    handler="notify",
                    params={
                        "channel": "ops",
                        "export_bucket": "exports",
                        "name_template": "report-{window}.json",
                    },
                )
            )
        )
        workspace = Workspace()
        with self.assertRaises(Exception) as ctx:
            notify(_ctx(catalog.job("notify-ops"), workspace))
        self.assertIn("missing", str(ctx.exception))
