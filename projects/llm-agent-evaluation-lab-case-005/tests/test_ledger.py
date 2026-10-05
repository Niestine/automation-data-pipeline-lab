"""The manifest is frozen before gold is opened."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import EXAMPLES, outcome
from repair_gate.harness import GoldJoinError
from repair_gate.suite import execute_suite
from repair_gate.util import canonical

SENTINEL = "GOLD-ONLY-SENTINEL-7f3a"
PATCH_MARK = "GOLD-PATCH-9c2e"


class LedgerTests(unittest.TestCase):
    def test_two_runs_share_one_manifest(self):
        first = outcome()["manifest"]
        second = execute_suite(EXAMPLES, EXAMPLES / "gold.json", None, write_manifest=False)
        self.assertEqual(first["manifest_sha256"], second["manifest"]["manifest_sha256"])
        text = canonical(first)
        self.assertNotIn("timestamp", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(PATCH_MARK, text)
        self.assertEqual(first["payload"]["seed"], 5)
        prompts = "\n".join(outcome()["report"]["prompts"])
        self.assertNotIn(SENTINEL, prompts)
        self.assertNotIn(PATCH_MARK, prompts)

    def test_missing_gold_does_not_rewrite_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text("prior", encoding="utf-8")
            with self.assertRaises(GoldJoinError):
                execute_suite(
                    EXAMPLES,
                    Path(tmp) / "missing-gold.json",
                    path,
                    write_manifest=True,
                )
            written = path.read_text(encoding="utf-8")
            self.assertNotIn(SENTINEL, written)
            self.assertNotIn("prior", written)
            digest = written
            with self.assertRaises(GoldJoinError):
                execute_suite(
                    EXAMPLES,
                    Path(tmp) / "missing-gold.json",
                    path,
                    write_manifest=True,
                )
            self.assertEqual(path.read_text(encoding="utf-8"), digest)

    def test_gold_changes_the_score_and_not_the_manifest(self):
        baseline = outcome()
        with tempfile.TemporaryDirectory() as tmp:
            examples = Path(tmp) / "examples"
            shutil.copytree(EXAMPLES, examples)
            gold = json.loads((examples / "gold.json").read_text(encoding="utf-8"))
            gold["cases"]["ep-reason-strict"]["answer"] = "41"
            gold["cases"]["ep-class-strict"]["answer"] = "foreign"
            (examples / "gold.json").write_text(json.dumps(gold), encoding="utf-8")
            shifted = execute_suite(examples, examples / "gold.json", None, write_manifest=False)
        self.assertEqual(
            shifted["manifest"]["manifest_sha256"],
            baseline["manifest"]["manifest_sha256"],
        )
        self.assertEqual(baseline["report"]["intersection_task_exact_match"], 1.0)
        self.assertEqual(shifted["report"]["intersection_task_exact_match"], 0.0)


if __name__ == "__main__":
    unittest.main()
