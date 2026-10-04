import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import helpers  # noqa: F401
from helpers import EXAMPLES, ROOT

from slip_lab.__main__ import check_exit, main
from slip_lab.logging_setup import teardown
from slip_lab.paths import CORPUS


class CliTests(unittest.TestCase):
    def tearDown(self):
        teardown()

    def _run(self, argv):
        stdout = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(argv, stdout=stdout)
        return code, json.loads(stdout.getvalue())

    def test_check_is_read_only_and_green(self):
        with tempfile.TemporaryDirectory() as directory:
            code, payload = self._run(["--log-dir", directory, "--run-id", "check"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome_coverage"], 1.0)
        self.assertEqual(payload["disposition_coverage"], 1.0)
        self.assertEqual(payload["metamorphic_violations"], 0)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["priority"][0], "CRASH")

    def test_list_defects_and_shrink_demo(self):
        code, payload = self._run(["--list-defects"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["defects"][0]["id"], "SLIP-001")
        self.assertEqual(len(payload["defects"]), 7)
        with tempfile.TemporaryDirectory() as directory:
            code, demo = self._run(["--shrink-demo", "--log-dir", directory])
        self.assertEqual(code, 0)
        self.assertLess(demo["reduced_length"], demo["parent_length"])
        self.assertEqual(demo["reduced_hex"], b'""""'.hex())

    def test_splice_dry_run_does_not_write_and_cap_zero_overflows(self):
        before = sorted(path.name for path in (CORPUS / "committed").glob("*.slip"))
        with tempfile.TemporaryDirectory() as directory:
            code, payload = self._run(["--splice", "--seed", "1", "--cap", "0", "--log-dir", directory])
        self.assertEqual(code, 0)
        self.assertGreaterEqual(payload["overflow"], 1)
        self.assertEqual(payload["committed"], 0)
        self.assertEqual(payload["malformed_probes"], 1)
        self.assertEqual(payload["written"], [])
        self.assertTrue(payload["dry_run"])
        after = sorted(path.name for path in (CORPUS / "committed").glob("*.slip"))
        self.assertEqual(before, after)

    def test_commit_writes_only_the_requested_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            commit_dir = Path(directory) / "out"
            code, payload = self._run(
                ["--splice", "--seed", "1", "--cap", "1", "--commit", "--commit-dir", str(commit_dir), "--log-dir", directory]
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["committed"], 1)
            self.assertEqual(len(list(commit_dir.glob("*.slip"))), 1)

    def test_blob_and_isolate_one(self):
        with tempfile.TemporaryDirectory() as directory:
            code, payload = self._run(["--blob", str(EXAMPLES / "lane_batch.slip"), "--log-dir", directory])
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "AGREE")
        with tempfile.TemporaryDirectory() as directory:
            code, isolated = self._run(
                ["--isolate-one", str(EXAMPLES / "lane_batch.slip"), "--timeout", "8", "--log-dir", directory]
            )
        self.assertEqual(code, 0)
        self.assertEqual(isolated["outcome"], "AGREE")

    def test_bad_log_dir_exits_2(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "file.log"
            blocked.write_text("x", encoding="utf-8")
            code, payload = self._run(["--log-dir", str(blocked)])
        self.assertEqual(code, 2)
        self.assertIn("error", payload)

    def test_run_lab_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            done = subprocess.run(
                [sys.executable, str(ROOT / "run_lab.py"), "--log-dir", directory, "--run-id", "e2e"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            log_text = (Path(directory) / "slip-e2e.log").read_text(encoding="utf-8")
        self.assertEqual(done.returncode, 0, done.stderr)
        head, brace, tail = done.stdout.partition("\n{")
        info = head.splitlines()
        self.assertEqual(len(info), 16)
        self.assertTrue(all(line.startswith("INFO") for line in info))
        payload = json.loads(brace.strip() + tail)
        self.assertEqual(payload["gate_problems"], [])
        self.assertEqual(Path(payload["log_file"]).name, "slip-e2e.log")
        self.assertEqual(done.stderr.count("ERROR"), 2)
        self.assertNotIn("INFO", done.stderr)
        self.assertIn("DEBUG", log_text)
        self.assertNotIn("DEBUG", done.stdout + done.stderr)

    def test_bad_requests_exit_2_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            cases = [
                ["--splice", "--commit"],
                ["--splice", "--commit", "--commit-dir", str(CORPUS / "committed")],
                ["--splice", "--cap", "-1"],
                ["--isolate-one", str(EXAMPLES / "lane_batch.slip"), "--timeout", "0"],
                ["--blob", str(Path(directory) / "missing.slip")],
            ]
            before = sorted(path.name for path in (CORPUS / "committed").iterdir())
            for argv in cases:
                code, payload = self._run(argv + ["--log-dir", directory])
                self.assertEqual(code, 2, argv)
                self.assertIn("error", payload, argv)
            self.assertEqual(before, sorted(path.name for path in (CORPUS / "committed").iterdir()))

    def test_check_exit_is_red_when_a_metric_fails(self):
        green = {
            "disposition_coverage": 1.0,
            "gate_problems": [],
            "metamorphic_violations": 0,
            "outcome_coverage": 1.0,
        }
        self.assertEqual(check_exit(green), 0)
        self.assertEqual(check_exit({**green, "metamorphic_violations": 1}), 1)
        self.assertEqual(check_exit({**green, "gate_problems": ["SLIP-006: accept_compat drift"]}), 1)
        self.assertEqual(check_exit({**green, "disposition_coverage": 0.5}), 1)


if __name__ == "__main__":
    unittest.main()
