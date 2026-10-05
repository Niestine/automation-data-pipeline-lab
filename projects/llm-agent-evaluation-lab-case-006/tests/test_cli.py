"""Command line entry for the tide desk."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PROJECT


class CliTests(unittest.TestCase):
    def test_default_run_prints_the_route_table(self):
        completed = subprocess.run(
            [sys.executable, str(PROJECT / "run_lab.py")],
            cwd=PROJECT.parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("validation_kept skiff,barque", completed.stdout)
        self.assertIn("Q1 skiff", completed.stdout)
        self.assertIn("Q2 yawl", completed.stdout)
        self.assertIn("Q3 barque", completed.stdout)
        self.assertIn("product_aiq", completed.stdout)
        self.assertIn("zero_aiq", completed.stdout)
        self.assertIn("dry_run false", completed.stdout)

    def test_dry_run_does_not_write_the_report(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.json"
            completed = subprocess.run(
                [sys.executable, str(PROJECT / "run_lab.py"), "--dry-run", "--out", str(out)],
                cwd=PROJECT.parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("dry_run true", completed.stdout)
            self.assertFalse(out.exists())

    def test_report_file_and_bad_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.json"
            completed = subprocess.run(
                [sys.executable, str(PROJECT / "run_lab.py"), "--out", str(out)],
                cwd=PROJECT.parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertGreater(payload["product_aiq"], payload["zero_aiq"])
            bad = Path(temp) / "examples"
            bad.mkdir()
            (bad / "validation_split.json").write_text("{", encoding="utf-8")
            (bad / "eval_fixture.json").write_text("{}", encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, str(PROJECT / "run_lab.py"), "--examples", str(bad)],
                cwd=PROJECT.parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertIn("fixture_error", failed.stderr)

            (bad / "validation_split.json").write_text(
                '{"costs": {}, "answers": {}}', encoding="utf-8"
            )
            schema = subprocess.run(
                [sys.executable, str(PROJECT / "run_lab.py"), "--examples", str(bad)],
                cwd=PROJECT.parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(schema.returncode, 2)
            self.assertEqual(schema.stderr.strip(), "fixture_error KeyError")


if __name__ == "__main__":
    unittest.main()
