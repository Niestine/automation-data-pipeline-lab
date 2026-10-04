import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
from helpers import EXAMPLES, ROOT

from web_collection_lab.__main__ import main, run_lab
from web_collection_lab.seed import build_catalog, build_fault_script, build_previous_snapshot, build_robots_txt


class CliTests(unittest.TestCase):
    def test_examples_match_the_seed_builders(self):
        catalog = json.loads((EXAMPLES / "catalog.json").read_text(encoding="utf-8"))
        faults = json.loads((EXAMPLES / "fault_script.json").read_text(encoding="utf-8"))
        snapshot = json.loads((EXAMPLES / "previous_snapshot.json").read_text(encoding="utf-8"))
        robots = (EXAMPLES / "robots.txt").read_text(encoding="utf-8")
        self.assertEqual(catalog, build_catalog())
        self.assertEqual(faults, build_fault_script())
        self.assertEqual(snapshot, build_previous_snapshot())
        self.assertEqual(robots, build_robots_txt())

    def test_default_run_completes_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_lab(["--state-dir", tmp])
            self.assertEqual(payload["checkpoint"]["status"], "complete")
            self.assertEqual(payload["listing_pages"], 2)
            self.assertEqual(payload["products_parsed"], 6)
            self.assertEqual(payload["products_rejected"], 1)
            self.assertEqual(payload["robots_skipped"], 1)
            self.assertEqual(payload["retries"], 3)
            self.assertEqual(payload["added"], 3)
            self.assertEqual(payload["removed"], 1)
            self.assertEqual(payload["changed"], 1)
            self.assertEqual(payload["unchanged"], 2)
            self.assertEqual(payload["catalog_size"], 6)
            self.assertEqual(payload["csv_rows"], 6)
            csv_text = (Path(tmp) / "lab-collect.csv").read_text(encoding="utf-8")
            self.assertIn("Café Apron", csv_text)
            self.assertIn("Wool Coat, Lined", csv_text)
            snapshot = json.loads((Path(tmp) / "lab-collect.snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(len(snapshot["products"]), 6)

    def test_dry_run_writes_nothing(self):
        payload = run_lab(["--dry-run"])
        self.assertTrue(payload["dry_run"])
        self.assertIsNone(payload["state_dir"])
        self.assertEqual(payload["products_parsed"], 6)
        self.assertEqual(payload["catalog_size"], 6)

    def test_log_jsonl_is_ascii_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = StringIO()
            with mock.patch("sys.stderr", stderr), mock.patch("sys.stdout", StringIO()):
                code = main(["--state-dir", tmp, "--log-jsonl"])
            self.assertEqual(code, 0)
            lines = stderr.getvalue().splitlines()
            self.assertTrue(lines)
            for line in lines:
                line.encode("ascii")
                record = json.loads(line)
                self.assertIn("event", record)
                self.assertIn("ts_ms", record)
            events = {json.loads(line)["event"] for line in lines}
            self.assertIn("robots_denied", events)
            self.assertIn("request_retry", events)

    def test_missing_input_exits_2(self):
        missing = ROOT / "does-not-exist.json"
        stderr = StringIO()
        with mock.patch("sys.stderr", stderr):
            code = main(["--catalog", str(missing)])
        self.assertEqual(code, 2)
        self.assertIn("could not load", stderr.getvalue())

    def test_malformed_input_rows_exit_2(self):
        cases = [
            ("--catalog", [1]),
            ("--catalog", [{"title": "x"}]),
            ("--faults", [{}]),
            ("--faults", [{"method": "GET", "path": "/catalog", "status": "503"}]),
            ("--snapshot", [1]),
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

    def test_out_of_range_burst_is_a_usage_error(self):
        with mock.patch("sys.stderr", StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--burst", "0"])
        self.assertEqual(ctx.exception.code, 2)

    def test_corrupt_checkpoint_exits_1_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lab-collect.checkpoint.json").write_text("{not json", encoding="utf-8")
            stderr = StringIO()
            with mock.patch("sys.stderr", stderr):
                code = main(["--state-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("checkpoint_error", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_crash_exit_code_3_leaves_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            faults = Path(tmp) / "faults.json"
            faults.write_text("[]", encoding="utf-8")
            stdout = StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "--state-dir",
                        tmp,
                        "--crash-after-products",
                        "1",
                        "--faults",
                        str(faults),
                    ]
                )
            self.assertEqual(code, 3)
            self.assertIn("simulated_crash", stdout.getvalue())
            checkpoint = json.loads((Path(tmp) / "lab-collect.checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
