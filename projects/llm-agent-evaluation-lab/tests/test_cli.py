import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import helpers
from llm_agent_lab.__main__ import default_examples_dir, main, run_lab


class CliTests(unittest.TestCase):
    def test_run_lab_heuristic_offline(self):
        payload = run_lab([])
        self.assertEqual(payload["provider"], "heuristic")
        self.assertEqual(payload["evaluation"]["status_accuracy"], 1.0)
        self.assertEqual(payload["mutations"], [])
        self.assertIn("run_start", payload["log_events"])

    def test_run_lab_fake_provider_script(self):
        payload = run_lab(["--provider", "fake"])
        self.assertEqual(payload["provider"], "fake")
        self.assertEqual(payload["evaluation"]["mean_score"], 1.0)

    def test_dry_run_flag_marks_auto_allow_tools(self):
        payload = run_lab(["--dry-run"])
        lookup = next(item for item in payload["results"] if item["ticket_id"] == "T-1001")
        denied = next(item for item in payload["results"] if item["ticket_id"] == "T-1003")
        blocked = next(item for item in payload["results"] if item["ticket_id"] == "T-1006")
        self.assertEqual(lookup["status"], "dry_run")
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(payload["mutations"], [])

    def test_approve_completes_admin_update_and_export(self):
        payload = run_lab(["--approve"])
        update = next(item for item in payload["results"] if item["ticket_id"] == "T-1004")
        export = next(item for item in payload["results"] if item["ticket_id"] == "T-1008")
        intern = next(item for item in payload["results"] if item["ticket_id"] == "T-1003")
        self.assertEqual(update["status"], "completed")
        self.assertEqual(export["status"], "completed")
        self.assertEqual(intern["status"], "denied")
        self.assertTrue(payload["mutations"])

    def test_main_prints_json(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([])
        self.assertEqual(code, 0)
        parsed = json.loads(buffer.getvalue())
        self.assertIn("evaluation", parsed)

    def test_missing_input_file_exits_2_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope.json")
            stderr = io.StringIO()
            with patch("sys.stderr", new=stderr), redirect_stdout(io.StringIO()):
                code = main(["--tickets", missing])
        self.assertEqual(code, 2)
        self.assertIn("could not load lab inputs", stderr.getvalue())

    def test_malformed_ticket_rows_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "tickets.json"
            bad.write_text(json.dumps([{"ticket_id": "T-1", "requester_role": "root"}]), encoding="utf-8")
            with patch("sys.stderr", new=io.StringIO()), redirect_stdout(io.StringIO()):
                code = main(["--tickets", str(bad)])
        self.assertEqual(code, 2)

    def test_default_examples_dir_resolves_inside_project(self):
        self.assertEqual(default_examples_dir().resolve(), helpers.EXAMPLES.resolve())

    def test_unknown_flag_exits(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            run_lab(["--not-a-flag"])


if __name__ == "__main__":
    unittest.main()
