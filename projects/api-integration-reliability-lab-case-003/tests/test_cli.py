"""CLI crash, resume, and dry-run."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts src on sys.path)
from plot_allotment.__main__ import main

ROOT = Path(__file__).resolve().parents[1]
EXPORTED = ["lot-001", "lot-002", "lot-004", "lot-005", "lot-006", "lot-007"]


def run_cli(argv: list[str]) -> tuple[int, dict | None, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


class CliTests(unittest.TestCase):
    def test_default_demo_recovers_the_gap(self) -> None:
        code, report, _err = run_cli([])
        self.assertEqual(code, 0)
        self.assertTrue(report["crashed_then_resumed"])
        self.assertTrue(report["replayed"])
        self.assertEqual(report["ledger_rows"], 6)
        self.assertEqual(report["duplicate_executions"], 0)
        self.assertEqual(report["seen_ids"], EXPORTED)
        self.assertIn("checkpoint_io", report["faults"])
        self.assertIn("gap_detected", report["faults"])

    def test_crash_then_resume_on_a_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = str(Path(tmp) / "state")
            crashed, stop, _err = run_cli(["--state", state, "--crash-before-checkpoint"])
            self.assertEqual(crashed, 3)
            self.assertTrue(stop["crashed"])
            self.assertTrue(stop["apply_ahead"])
            self.assertEqual(stop["ledger_rows"], 2)

            resumed, report, _err = run_cli(["--state", state])
            self.assertEqual(resumed, 0)
            self.assertFalse(report["crashed_then_resumed"])
            self.assertTrue(report["replayed"])
            self.assertEqual(report["pages_this_run"], 3)
            self.assertEqual(report["ledger_rows"], 6)
            self.assertEqual(report["duplicate_executions"], 0)
            self.assertEqual(report["seen_ids"], EXPORTED)

            finished, again, _err = run_cli(["--state", state])
            self.assertEqual(finished, 0)
            self.assertEqual(again["pages_this_run"], 0)
            self.assertEqual(again["ledger_rows"], 6)
            self.assertEqual(again["duplicate_executions"], 0)

    def test_crash_flag_requires_state(self) -> None:
        code, report, err = run_cli(["--crash-before-checkpoint"])
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertTrue(err.startswith("error:"))

    def test_dry_run_lists_pages_without_writing(self) -> None:
        code, report, _err = run_cli(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["ledger_rows"], 0)
        self.assertEqual(report["pages_this_run"], 3)
        self.assertEqual(report["seen_ids"], EXPORTED)

    def test_missing_and_malformed_examples_exit_2(self) -> None:
        code, _report, err = run_cli(["--allotments", str(ROOT / "examples" / "missing.json")])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"))
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            document = json.loads((ROOT / "examples" / "allotments.json").read_text(encoding="utf-8"))
            document["resources"][3]["beds"] = "two"
            bad.write_text(json.dumps(document), encoding="utf-8")
            state = Path(tmp) / "state"
            code, report, err = run_cli(["--allotments", str(bad), "--state", str(state)])
            self.assertEqual(code, 2)
            self.assertIsNone(report)
            self.assertIn("beds", err)

    def test_examples_are_synthetic_json(self) -> None:
        document = json.loads((ROOT / "examples" / "allotments.json").read_text(encoding="utf-8"))
        self.assertEqual(document["parent"], "garden-north")
        self.assertEqual(len(document["resources"]), 7)
        for name in ("allotments.json", "fault_script.json", "webhook_event.json"):
            self.assertNotIn("whsec_", (ROOT / "examples" / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
