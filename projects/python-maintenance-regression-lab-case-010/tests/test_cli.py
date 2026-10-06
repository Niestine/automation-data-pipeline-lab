"""The offline command line checks the corpus and writes no witnesses."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

import helpers  # noqa: F401
from helpers import CORPUS, EXAMPLES, ROOT, corpus_digest


class CliTests(unittest.TestCase):
    def test_check_reports_a_clean_corpus(self):
        self.assertEqual(len(list(CORPUS.glob("*.bin"))), 39)
        before = corpus_digest()
        completed = subprocess.run(
            [sys.executable, str(ROOT / "run_lab.py"), "check"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"cases": 39, "problems": 0})
        # Expected rejections are not alarms on a clean run.
        self.assertEqual(completed.stderr, "")
        self.assertEqual(corpus_digest(), before)

        verbose = subprocess.run(
            [sys.executable, str(ROOT / "run_lab.py"), "check", "--verbose"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verbose.returncode, 0, verbose.stderr)
        self.assertIn("WARNING wharf_sheet code=E-bare-cr decision=D-crlf record=0 field=1 byte=7", verbose.stderr)

    def test_parse_accepts_the_strict_example_and_rejects_the_comment(self):
        accepted = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_lab.py"),
                "parse",
                str(EXAMPLES / "intake_strict.csv"),
                "--header",
                "present",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        payload = json.loads(accepted.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["header"], ["berth", "sku", "qty", "note"])
        self.assertEqual(payload["records"][1][1], "HOOK, SMALL")
        self.assertEqual(payload["events"], [])

        rejected = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_lab.py"),
                "parse",
                str(EXAMPLES / "legacy_comment.bin"),
                "--profile",
                "hardened",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 1, rejected.stdout)
        failure = json.loads(rejected.stdout)
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["code"], "E-comment")
        self.assertEqual(failure["decision_id"], "D-comments")

    def test_legacy_parse_logs_the_repair_on_stderr_without_field_text(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_lab.py"),
                "parse",
                str(EXAMPLES / "legacy_comment.bin"),
                "--profile",
                "legacy",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["records"], [["N-1", "ROPE-4"]])
        self.assertEqual(
            payload["events"],
            [{"byte_offset": 0, "decision_id": "D-comments", "field_index": 0, "record_index": 0}],
        )
        self.assertIn("INFO wharf_sheet repair decision=D-comments record=0 field=0 byte=0", completed.stderr)
        self.assertNotIn("skip me", completed.stderr)

    def test_bad_arguments_exit_2_without_a_traceback(self):
        for extra in (["--field-limit", "0"], ["--field-limit", "-3"]):
            completed = subprocess.run(
                [sys.executable, str(ROOT / "run_lab.py"), "parse", str(EXAMPLES / "intake_strict.csv"), *extra],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertNotIn("Traceback", completed.stderr)
        missing = subprocess.run(
            [sys.executable, str(ROOT / "run_lab.py"), "parse", str(EXAMPLES / "no_such_sheet.csv")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn("Traceback", missing.stderr)

    def test_differential_command_writes_nothing(self):
        before = corpus_digest()
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_lab.py"),
                "differential",
                "--seed",
                "20261006",
                "--budget",
                "8",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["files_written"], 0)
        self.assertEqual(payload["unclassified"], 0)
        self.assertEqual(corpus_digest(), before)
