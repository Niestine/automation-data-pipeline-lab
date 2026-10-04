import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import helpers
from brief_router_lab.__main__ import default_examples_dir, main, run_lab


class CliTests(unittest.TestCase):
    def test_run_lab_heuristic_offline(self):
        payload = run_lab([])
        self.assertEqual(payload["provider"], "heuristic")
        self.assertEqual(payload["evaluation"]["status_accuracy"], 1.0)
        self.assertEqual(payload["evaluation"]["mean_score"], 1.0)
        self.assertTrue(payload["mutations"])
        self.assertIn("run_start", payload["log_events"])
        queued = [item["draft_id"] for item in payload["publish_queue"]]
        self.assertEqual(queued, ["DRF-P-2003"])

    def test_run_lab_fake_provider_script(self):
        payload = run_lab(["--provider", "fake"])
        self.assertEqual(payload["provider"], "fake")
        self.assertEqual(payload["evaluation"]["mean_score"], 1.0)
        self.assertEqual(payload["evaluation"]["sequence_accuracy"], 1.0)

    def test_dry_run_flag_skips_mutations(self):
        payload = run_lab(["--dry-run"])
        lookup = next(item for item in payload["results"] if item["packet_id"] == "P-2001")
        denied = next(item for item in payload["results"] if item["packet_id"] == "P-2004")
        blocked = next(item for item in payload["results"] if item["packet_id"] == "P-2006")
        self.assertEqual(lookup["status"], "dry_run")
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(payload["mutations"], [])
        self.assertEqual(payload["publish_queue"], [])

    def test_approve_does_not_lift_role_denials(self):
        payload = run_lab(["--approve"])
        viewer = next(item for item in payload["results"] if item["packet_id"] == "P-2004")
        publish = next(item for item in payload["results"] if item["packet_id"] == "P-2003")
        self.assertEqual(viewer["status"], "denied")
        self.assertEqual(publish["status"], "completed")
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
                code = main(["--packets", missing])
        self.assertEqual(code, 2)
        self.assertTrue(stderr.getvalue().startswith("error:"))
        self.assertNotIn("Traceback", stderr.getvalue())

    def _exit_for_input(self, flag, content, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(content, encoding="utf-8")
            stderr = io.StringIO()
            with patch("sys.stderr", new=stderr), redirect_stdout(io.StringIO()):
                code = main([*extra, flag, str(path)])
        return code, stderr.getvalue()

    def test_malformed_script_exits_2(self):
        code, err = self._exit_for_input("--script", "[1]", "--provider", "fake")
        self.assertEqual(code, 2)
        self.assertIn("script", err)
        code, _ = self._exit_for_input("--script", '{"P-2001": "not-a-list"}', "--provider", "fake")
        self.assertEqual(code, 2)

    def test_malformed_faults_exits_2(self):
        code, err = self._exit_for_input("--faults", '{"P-2001": [1]}')
        self.assertEqual(code, 2)
        self.assertIn("faults", err)

    def test_unclassified_workspace_asset_exits_2(self):
        code, err = self._exit_for_input("--workspace", '{"assets": {"AST-101": {"title": "x"}}}')
        self.assertEqual(code, 2)
        self.assertIn("classification", err)

    def test_fault_script_drives_retries_through_cli(self):
        faults = '{"P-2008": [{"tool": "catalog.get", "error": true, "code": "timeout", "transient": true}]}'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "faults.json"
            path.write_text(faults, encoding="utf-8")
            payload = run_lab(["--faults", str(path)])
        lookup = next(item for item in payload["results"] if item["packet_id"] == "P-2008")
        self.assertEqual(lookup["status"], "completed")
        self.assertEqual(lookup["step_results"][0]["attempts"], 2)
        self.assertIn("step_retry", payload["log_events"])

    def test_default_examples_dir_exists(self):
        folder = default_examples_dir()
        self.assertTrue((folder / "packets.json").is_file())
        self.assertEqual(folder, helpers.EXAMPLES)
