"""The project entry point runs offline and writes only where it is told."""

from __future__ import annotations

import json
import subprocess
import sys
import sysconfig
import tempfile
import helpers  # noqa: F401
import unittest
from pathlib import Path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(helpers.ROOT / "run_lab.py"), *args],
        capture_output=True,
        text=True,
        cwd=str(helpers.ROOT),
    )


class CliTests(unittest.TestCase):
    def test_budget_prints_the_depth_bound(self) -> None:
        completed = _run("budget", "--n", "2", "--k", "4", "--d", "2")
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("p=", completed.stdout)
        self.assertIn("R=", completed.stdout)
        self.assertIn("d=2", completed.stdout)

    def test_replay_of_the_checked_in_atomicity_schedule(self) -> None:
        artifact = str(helpers.EXAMPLES / "atomicity_d2_buggy.json")
        fixed = _run("replay", artifact, "--revision", "fixed")
        self.assertEqual(fixed.returncode, 0, msg=fixed.stderr)
        self.assertIn("terminal=pass", fixed.stdout)
        buggy = _run("replay", artifact, "--revision", "buggy")
        self.assertEqual(buggy.returncode, 1, msg=buggy.stderr)
        self.assertIn("terminal=fail", buggy.stdout)

    def test_campaign_writes_under_the_given_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = _run(
                "campaign",
                "--subject",
                "ordering_d1",
                "--revision",
                "buggy",
                "--policy",
                "pct",
                "--d",
                "1",
                "--seeds",
                "0-19",
                "--n-max",
                "2",
                "--artifact-dir",
                tmp,
            )
            self.assertEqual(completed.returncode, 1, msg=completed.stderr)
            self.assertIn("terminal=fail", completed.stdout)
            self.assertNotIn("tid=", completed.stderr)
            self.assertIn("schedlab.campaign", completed.stderr)
            written = list(Path(tmp).glob("*.json"))
            self.assertGreaterEqual(len(written), 1)
            self.assertTrue(str(written[0]).startswith(tmp))

    def test_replay_writes_a_stamped_artifact_and_flags_divergence(self) -> None:
        artifact = str(helpers.EXAMPLES / "deadlock_d2_buggy.json")
        with tempfile.TemporaryDirectory() as tmp:
            buggy = _run("replay", artifact, "--revision", "buggy", "--artifact-dir", tmp)
            self.assertEqual(buggy.returncode, 1, msg=buggy.stderr)
            self.assertIn("terminal=deadlock", buggy.stdout)
            written = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(written), 1)
            payload = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["policy"], "replay")
        self.assertEqual(payload["terminal"], "deadlock")
        self.assertEqual(payload["schedule"], [1, 2, 2, 1])
        self.assertEqual(payload["python"], sys.version)
        self.assertEqual(payload["gil"]["Py_GIL_DISABLED"], sysconfig.get_config_var("Py_GIL_DISABLED"))
        self.assertNotEqual(payload["gil"].get("gil_probe"), "fixture")
        fixed = _run("replay", artifact, "--revision", "fixed")
        self.assertEqual(fixed.returncode, 2, msg=fixed.stderr)
        self.assertIn("terminal=diverged", fixed.stdout)

    def test_padded_campaign_artifact_replays_on_the_padded_twin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = _run(
                "campaign",
                "--subject",
                "atomicity_d2",
                "--revision",
                "buggy",
                "--policy",
                "preemption",
                "--n-max",
                "2",
                "--pad",
                "1",
                "--artifact-dir",
                tmp,
            )
            self.assertEqual(completed.returncode, 1, msg=completed.stderr)
            failing = [
                path
                for path in Path(tmp).glob("atomicity_d2-pad1-*.json")
                if json.loads(path.read_text(encoding="utf-8"))["terminal"] == "fail"
            ]
            self.assertEqual(len(failing), 1)
            self.assertEqual(json.loads(failing[0].read_text(encoding="utf-8"))["pad"], 1)
            fixed = _run("replay", str(failing[0]), "--revision", "fixed")
        self.assertEqual(fixed.returncode, 0, msg=fixed.stdout + fixed.stderr)
        self.assertIn("terminal=pass", fixed.stdout)

    def test_smoke_command_is_informational(self) -> None:
        completed = _run("smoke")
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("informational", completed.stdout)
        self.assertIn("false", completed.stdout.lower())


if __name__ == "__main__":
    unittest.main()
