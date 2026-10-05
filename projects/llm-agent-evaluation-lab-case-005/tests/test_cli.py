"""Command line from the repository root."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

RUN = "projects/llm-agent-evaluation-lab-case-005/run_lab.py"
WORDS = (
    "format_success",
    "task_exact_match",
    "raw",
    "loc_obs",
    "full_prose",
    "full_keyed",
    "benign_utility",
    "utility_under_attack",
    "targeted_asr",
    "block_all",
    "unsafe_dispatch",
    "value_accuracy",
)


class CliTests(unittest.TestCase):
    def _run(self, args, cwd=None):
        return subprocess.run(
            [sys.executable, RUN, *args],
            cwd=cwd or REPO,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_default_run_prints_the_table(self):
        proc = self._run([])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for word in WORDS:
            self.assertIn(word, proc.stdout)

    def test_dry_run_does_not_write_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            proc = self._run(["--dry-run", "--manifest", str(manifest)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("dry_run=true", proc.stdout)
            self.assertFalse(manifest.exists())

    def test_manifest_flag_writes_the_printed_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            proc = self._run(["--manifest", str(manifest)])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            written = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIn(f"manifest_sha256 {written['manifest_sha256']}", proc.stdout)
            self.assertEqual(len(written["payload"]["episodes"]), 47)

    def test_missing_gold_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            proc = self._run(["--gold", str(missing)])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("gold join failed", proc.stderr)

    def test_bad_json_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "gold.json"
            bad.write_text("{", encoding="utf-8")
            proc = self._run(["--gold", str(bad)])
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
