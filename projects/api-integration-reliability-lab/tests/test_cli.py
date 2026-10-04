import json
import tempfile
import unittest
from pathlib import Path
from io import StringIO
from unittest import mock

import helpers  # noqa: F401
from helpers import EXAMPLES, ROOT

from api_reliability_lab.__main__ import main, run_lab
from api_reliability_lab.seed import build_catalog, build_fault_script, build_webhook_events


class CliTests(unittest.TestCase):
    def test_examples_match_the_seed_builders(self):
        catalog = json.loads((EXAMPLES / "catalog.json").read_text(encoding="utf-8"))
        faults = json.loads((EXAMPLES / "fault_script.json").read_text(encoding="utf-8"))
        events = json.loads((EXAMPLES / "webhook_events.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog, build_catalog())
        self.assertEqual(faults, build_fault_script())
        self.assertEqual(events, build_webhook_events())

    def test_default_run_completes_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_lab(["--checkpoint-dir", tmp, "--faults", str(EXAMPLES / "fault_script.json")])
        self.assertEqual(payload["checkpoint"]["status"], "complete")
        self.assertEqual(payload["ledger_size"], 25)
        self.assertEqual(payload["orders_inserted"], 24)
        self.assertEqual(payload["webhooks_applied"], 3)
        self.assertEqual(payload["webhooks_duplicate"], 2)
        self.assertEqual(payload["webhooks_stale"], 1)
        self.assertEqual(payload["retries"], 5)
        self.assertEqual(payload["acks_sent"], 4)
        self.assertEqual(payload["acks_replayed"], 1)
        self.assertEqual(payload["ack_state_size"], 5)

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
            ("--catalog", [{"status": "paid"}]),
            ("--faults", [{}]),
            ("--faults", [{"method": "GET", "path": "/v1/orders", "status": "503"}]),
            ("--webhooks", ["evt"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            for flag, payload in cases:
                with self.subTest(flag=flag, payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    stderr = StringIO()
                    with mock.patch("sys.stderr", stderr):
                        code = main([flag, str(path), "--checkpoint-dir", tmp])
                    self.assertEqual(code, 2)
                    self.assertTrue(stderr.getvalue().strip())

    def test_out_of_range_page_limit_is_a_usage_error(self):
        with mock.patch("sys.stderr", StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--page-limit", "0"])
        self.assertEqual(ctx.exception.code, 2)

    def test_corrupt_checkpoint_exits_1_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lab-sync.json").write_text("{not json", encoding="utf-8")
            stderr = StringIO()
            with mock.patch("sys.stderr", stderr):
                code = main(["--checkpoint-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("checkpoint_error", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_simulated_crash_exits_3_and_resume_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "--checkpoint-dir",
                        tmp,
                        "--crash-after-pages",
                        "1",
                        "--faults",
                        str(EXAMPLES / "fault_script.json"),
                        "--webhooks",
                        str(EXAMPLES / "webhook_events.json"),
                    ]
                )
            self.assertEqual(code, 3)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["error"], "simulated_crash")

            stdout = StringIO()
            with mock.patch("sys.stdout", stdout):
                code = main(
                    [
                        "--checkpoint-dir",
                        tmp,
                        "--faults",
                        str(EXAMPLES / "fault_script.json"),
                        "--webhooks",
                        str(EXAMPLES / "webhook_events.json"),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["resumed"])
            self.assertEqual(payload["ledger_size"], 25)
            self.assertEqual(payload["checkpoint"]["status"], "complete")

    def test_dry_run_flag_does_not_write_checkpoint_or_ledger_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = run_lab(["--checkpoint-dir", tmp, "--dry-run"])
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["ledger_size"], 0)
            self.assertEqual(list(Path(tmp).glob("*")), [])


if __name__ == "__main__":
    unittest.main()
