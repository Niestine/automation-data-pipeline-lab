import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
from helpers import EXAMPLES, ROOT

from automation_job_lab.__main__ import main, run_lab
from automation_job_lab.models import LAB_EPOCH_MS
from automation_job_lab.seed import WINDOW_MS, build_catalog, build_fault_script, build_inbox


class CliTests(unittest.TestCase):
    def test_examples_match_the_seed_builders(self):
        catalog = json.loads((EXAMPLES / "catalog.json").read_text(encoding="utf-8"))
        inbox = json.loads((EXAMPLES / "inbox.json").read_text(encoding="utf-8"))
        faults = json.loads((EXAMPLES / "fault_script.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog, build_catalog())
        self.assertEqual(inbox, build_inbox(window=0))
        self.assertEqual(faults, build_fault_script())

    def test_default_run_completes_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_lab(["--state-dir", tmp])
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["retries"], 2)
        self.assertEqual(payload["windows_run"], 1)
        self.assertEqual(payload["workspace"]["inbox"], 2)
        self.assertEqual(payload["workspace"]["staging"], 12)
        self.assertEqual(payload["workspace"]["clean"], 10)
        self.assertEqual(payload["workspace"]["dead_letter"], 2)
        self.assertEqual(payload["workspace"]["archive"], 10)
        self.assertEqual(payload["workspace"]["exports"], 1)
        self.assertEqual(payload["workspace"]["notifications"], 1)
        self.assertEqual(payload["workspace"]["heartbeats"], 1)
        jobs = {item["job_id"]: item for item in payload["jobs"]}
        self.assertEqual(jobs["ingest-inbox"]["attempts"], 2)
        self.assertEqual(jobs["transform-records"]["attempts"], 2)
        self.assertEqual(len(payload["sleep_delays_ms"]), 2)

    def test_dry_run_does_not_write_durable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_lab(["--state-dir", tmp, "--dry-run"])
            self.assertEqual(payload["status"], "complete")
            self.assertTrue(payload["dry_run"])
            self.assertIsNone(payload["state_dir"])
            self.assertEqual(payload["workspace"]["staging"], 12)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_dry_run_without_state_dir_creates_no_temp_dir(self):
        with mock.patch("tempfile.mkdtemp") as mkdtemp:
            payload = run_lab(["--dry-run"])
        mkdtemp.assert_not_called()
        self.assertIsNone(payload["state_dir"])

    def test_failed_pipeline_exits_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            faults = Path(tmp) / "faults.json"
            faults.write_text(json.dumps([{"job_id": "ingest-inbox", "error": "validation"}]), encoding="utf-8")
            stdout = StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main(["--state-dir", str(Path(tmp) / "state"), "--faults", str(faults)])
        self.assertEqual(code, 4)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failed_windows"], 1)

    def test_log_jsonl_streams_events_to_stderr(self):
        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = main(["--state-dir", tmp, "--log-jsonl"])
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        names = [item["event"] for item in events]
        self.assertEqual(names[0], "checkpoint_saved")
        self.assertIn("job_retry", names)
        self.assertEqual(names[-1], "pipeline_complete")
        self.assertTrue(all(isinstance(item["ts_ms"], int) for item in events))
        self.assertEqual(json.loads(stdout.getvalue())["status"], "complete")

    def test_until_windows_ingests_the_next_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty-faults.json"
            empty.write_text("[]", encoding="utf-8")
            payload = run_lab(
                ["--state-dir", tmp, "--until-windows", "2", "--faults", str(empty)]
            )
        self.assertEqual(payload["windows_run"], 2)
        self.assertEqual(payload["workspace"]["clean"], 13)
        self.assertEqual(payload["workspace"]["archive"], 13)
        self.assertEqual(payload["workspace"]["exports"], 2)
        self.assertEqual(payload["workspace"]["heartbeats"], 2)
        self.assertEqual(len(payload["window_reports"]), 2)

    def test_missing_input_exits_2(self):
        missing = ROOT / "does-not-exist.json"
        stderr = StringIO()
        with mock.patch("sys.stderr", stderr):
            code = main(["--catalog", str(missing)])
        self.assertEqual(code, 2)
        self.assertIn("could not load", stderr.getvalue())

    def test_malformed_input_exits_2(self):
        cases = [
            ("--catalog", [1]),
            ("--catalog", {"pipeline": "x"}),
            ("--inbox", ["rec"]),
            ("--inbox", [{"status": "pending"}]),
            ("--faults", [{}]),
            ("--faults", [{"job_id": "ingest-inbox", "error": "nope"}]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            for flag, payload in cases:
                with self.subTest(flag=flag, payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    stderr = StringIO()
                    with mock.patch("sys.stderr", stderr):
                        code = main([flag, str(path), "--state-dir", tmp])
                    self.assertEqual(code, 2)
                    self.assertTrue(stderr.getvalue().strip())

    def test_out_of_range_until_windows_is_a_usage_error(self):
        with mock.patch("sys.stderr", StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--until-windows", "0"])
        self.assertEqual(ctx.exception.code, 2)

    def test_corrupt_checkpoint_exits_1_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            ckpt = Path(tmp) / "checkpoints"
            ckpt.mkdir()
            name = f"hourly-ops_w{LAB_EPOCH_MS}.json"
            (ckpt / name).write_text("{not json", encoding="utf-8")
            stderr = StringIO()
            with mock.patch("sys.stderr", stderr):
                code = main(["--state-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("checkpoint_error", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_crash_exits_3_and_resume_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "--state-dir",
                        tmp,
                        "--crash-after-jobs",
                        "2",
                        "--crash-at",
                        "post_checkpoint",
                    ]
                )
            self.assertEqual(code, 3)
            self.assertIn("simulated_crash", stdout.getvalue())
            payload = run_lab(["--state-dir", tmp])
        self.assertEqual(payload["status"], "complete")
        self.assertTrue(payload["resumed"])
        self.assertEqual(payload["workspace"]["archive"], 10)

    def test_now_ms_skips_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_lab(
                ["--state-dir", tmp, "--now-ms", str(LAB_EPOCH_MS + 13 * 60_000)]
            )
        jobs = {item["job_id"]: item for item in payload["jobs"]}
        self.assertEqual(jobs["heartbeat-log"]["status"], "skipped")
        self.assertEqual(jobs["heartbeat-log"]["reason"], "not_due")
        self.assertEqual(jobs["ingest-inbox"]["status"], "succeeded")

    def test_main_prints_json(self):
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.stdout", stdout):
                code = main(["--state-dir", tmp])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["window_ms"], WINDOW_MS)
