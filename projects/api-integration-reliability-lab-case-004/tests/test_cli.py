"""Demo report for the default run, dry run, and dropped refresh."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from io import StringIO
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from helpers import REPO, ROOT
import unittest

from lotcycle.demo import main
from lotcycle.world import load_examples, make_entries


REPORT_KEYS = {
    "checkpoints",
    "client_id",
    "client_status",
    "decisions",
    "dry_run",
    "entries",
    "orders",
    "reconstruction_incomplete",
    "refresh_posts",
    "server_generation",
    "server_status",
    "webhook_resource_found",
    "webhook_status",
}


def _run(argv: list[str]) -> tuple[int, str, str]:
    out = StringIO()
    err = StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class DemoTest(unittest.TestCase):
    def test_sample_entry_matches_the_generator(self) -> None:
        examples = load_examples()
        ledger = make_entries(examples["excursions"]["ledger"])
        alarms = make_entries(examples["excursions"]["alarms"])
        self.assertEqual(ledger[0], examples["excursions"]["ledger"]["samples"][0])
        self.assertEqual(ledger[0]["celsius"], -19)
        self.assertEqual(ledger[0]["id"], "ex-001")
        self.assertEqual(alarms[0]["id"], "al-001")
        self.assertEqual(len(ledger), 120)
        self.assertEqual(len(alarms), 3)

    def test_default_report(self) -> None:
        code, out, err = _run([])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        report = json.loads(out)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["client_id"], "handheld-north")
        self.assertEqual(report["refresh_posts"], 2)
        self.assertEqual(report["entries"], 120)
        self.assertEqual(report["orders"], 1)
        self.assertEqual(report["checkpoints"], 2)
        self.assertEqual(report["webhook_status"], 204)
        self.assertIs(report["webhook_resource_found"], True)
        self.assertEqual(report["server_generation"], 2)
        self.assertEqual(report["server_status"], "active")
        self.assertEqual(report["client_status"], "active")
        self.assertIs(report["dry_run"], False)
        self.assertIs(report["reconstruction_incomplete"], False)
        self.assertIn("refresh_precommit_retry", report["decisions"])
        self.assertIn("refresh_ok", report["decisions"])

    def test_dry_run_ignores_state_and_faults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _run(["--dry-run", "--state", tmp, "--drop-refresh"])
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertFalse((Path(tmp) / "lotcycle.sqlite").exists())
        report = json.loads(out)
        self.assertEqual(report["refresh_posts"], 0)
        self.assertEqual(report["entries"], 120)
        self.assertEqual(report["orders"], 0)
        self.assertEqual(report["checkpoints"], 0)
        self.assertEqual(report["server_generation"], 1)
        self.assertEqual(report["client_status"], "active")
        self.assertIsNone(report["webhook_status"])
        self.assertIsNone(report["webhook_resource_found"])
        self.assertIs(report["reconstruction_incomplete"], False)
        self.assertIs(report["dry_run"], True)

    def test_dropped_refresh_leaves_the_family_active(self) -> None:
        code, out, err = _run(["--drop-refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        report = json.loads(out)
        self.assertEqual(report["refresh_posts"], 1)
        self.assertEqual(report["client_status"], "reauth_required")
        self.assertEqual(report["server_status"], "active")
        self.assertEqual(report["server_generation"], 2)
        self.assertEqual(report["orders"], 0)
        self.assertEqual(report["entries"], 0)
        self.assertEqual(report["checkpoints"], 0)
        self.assertIn("refresh_post_send_stop", report["decisions"])
        self.assertIsNone(report["webhook_status"])

    def test_state_directory_keeps_orders_and_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _run(["--state", tmp])
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            database = Path(tmp) / "lotcycle.sqlite"
            self.assertTrue(database.is_file())
            connection = sqlite3.connect(database)
            try:
                orders = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                checkpoints = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(orders, 1)
            self.assertEqual(checkpoints, 2)
        report = json.loads(out)
        self.assertIn("refresh_precommit_retry", report["decisions"])
        self.assertIn("refresh_ok", report["decisions"])

    def test_malformed_examples_exit_2(self) -> None:
        with patch("lotcycle.demo.load_examples", side_effect=json.JSONDecodeError("bad", "doc", 0)):
            code, out, err = _run([])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error:"))

        with patch("lotcycle.demo.load_examples", side_effect=FileNotFoundError("examples/grants.json")):
            code, out, err = _run([])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "examples").mkdir()
            for name in ("webhook_event.json", "excursions.json", "fault_script.json", "grants.json"):
                (root / "examples" / name).write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_examples(root)

    def test_subprocess_dry_run_from_repository_root(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "run_lab.py"), "--dry-run"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["entries"], 120)
        self.assertEqual(report["refresh_posts"], 0)
        self.assertEqual(report["orders"], 0)
        self.assertIs(report["dry_run"], True)


if __name__ == "__main__":
    unittest.main()
