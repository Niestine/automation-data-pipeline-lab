"""CLI input failures and the metric table."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helpers import EXAMPLES

from office_gate_lab.__main__ import main


class CliTests(unittest.TestCase):
    def test_default_suite_prints_the_metric_lines(self) -> None:
        stdout = io.StringIO()
        code = main(["--trials", "1"], stdout=stdout)
        text = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("benign_utility=1.000", text)
        self.assertIn("utility_under_attack=1.000", text)
        self.assertIn("targeted_attack_success=0.000", text)
        self.assertIn("pass^k=1.000", text)
        self.assertIn("reliability_headline=pass^k", text)
        self.assertIn("label=utility_collapse", text)
        self.assertIn("dataflow_hijack task=t02-brief-field status=paused outbox_count=0", text)
        self.assertIn("dataflow_hijack_undefended task=t02-brief-field status=finished outbox_count=1", text)

    def test_other_task_files_run_and_bad_shapes_exit_2(self) -> None:
        payload = json.loads((EXAMPLES / "tasks.json").read_text(encoding="utf-8"))
        subset = [task for task in payload["tasks"] if task["id"] == "t03-roster-literal"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "subset.json"
            path.write_text(json.dumps({"tasks": subset}), encoding="utf-8")
            stdout = io.StringIO()
            self.assertEqual(main(["--tasks", str(path), "--trials", "1"], stdout=stdout), 0)
            text = stdout.getvalue()
            self.assertIn("tasks=1 k=1", text)
            self.assertNotIn("meeting_notes_pair", text)
            self.assertNotIn("dataflow_hijack", text)
            broken = [dict(subset[0])]
            del broken[0]["golden_outbox"]
            path.write_text(json.dumps({"tasks": broken}), encoding="utf-8")
            self.assertEqual(main(["--tasks", str(path)], stdout=io.StringIO()), 2)
            path.write_text(json.dumps({"tasks": subset + subset}), encoding="utf-8")
            self.assertEqual(main(["--tasks", str(path)], stdout=io.StringIO()), 2)
            world = Path(directory) / "world.json"
            world.write_text(json.dumps({"documents": [{"id": "doc-x"}], "messages": []}), encoding="utf-8")
            self.assertEqual(main(["--world", str(world)], stdout=io.StringIO()), 2)

    def test_missing_and_malformed_files_exit_2(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            stdout = io.StringIO()
            code = main(["--tasks", str(missing), "--world", str(EXAMPLES / "world.json")], stdout=stdout)
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            bad = Path(directory) / "bad.json"
            bad.write_text("{", encoding="utf-8")
            code = main(["--tasks", str(bad), "--world", str(EXAMPLES / "world.json")], stdout=io.StringIO())
            self.assertEqual(code, 2)
            code = main(["--trials", "0"], stdout=io.StringIO())
            self.assertEqual(code, 2)

    def test_shipped_tasks_are_ten_unique_requests(self) -> None:
        payload = json.loads((EXAMPLES / "tasks.json").read_text(encoding="utf-8"))
        requests = [task["request"] for task in payload["tasks"]]
        self.assertEqual(len(requests), 10)
        self.assertEqual(len(requests), len(set(requests)))


if __name__ == "__main__":
    unittest.main()
