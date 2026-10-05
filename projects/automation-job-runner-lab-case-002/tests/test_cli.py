"""CLI help, submit codes, dry-run, and the sample batch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path

import helpers
from shiftlease.cli import main
from shiftlease.contract import FINGERPRINT_ID


class CliTests(unittest.TestCase):
    def test_help_names_the_expired_draft_and_the_fingerprint(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "shiftlease", "--help"],
            cwd=str(helpers.ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("expired 18 April 2026", proc.stdout)
        self.assertIn("draft-ietf-httpapi-idempotency-key-header-07", proc.stdout)
        self.assertIn(FINGERPRINT_ID, proc.stdout)

    def test_submit_conflict_and_worker_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "queue.sqlite"
            outbox = root / "out"
            payload = json.dumps(helpers.slip())
            code, body = _run(
                ["--db", str(db), "submit", "--key", "cli-key-1", "--payload", payload, "--max-attempts", "3"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(body["outcome"], "created")
            self.assertEqual(body["http_class"], 201)
            code, body = _run(["--db", str(db), "submit", "--key", "cli-key-1", "--payload", payload])
            self.assertEqual(code, 4)
            self.assertEqual(body["outcome"], "conflict")
            self.assertEqual(body["attempts"], 0)
            code, _body = _run(["--db", str(db), "submit", "--payload", payload])
            self.assertEqual(code, 2)
            code, _body = _run(
                ["--db", str(db), "submit", "--key", "cli-key-1", "--payload", json.dumps(helpers.slip(qty=8))]
            )
            self.assertEqual(code, 2)
            code, body = _run(
                ["--db", str(db), "--outbox", str(outbox), "worker", "--max-jobs", "1", "--owner", "cli-worker", "--seed", "1"]
            )
            self.assertEqual(code, 0, body)
            self.assertEqual(body["jobs"], 1)
            self.assertEqual(len(list(outbox.glob("*.csv"))), 1)
            code, body = _run(["--db", str(db), "status"])
            self.assertEqual(code, 0)
            self.assertEqual(body["counts"]["succeeded"], 1)
            code, body = _run(
                ["--db", str(db), "--outbox", str(outbox), "worker", "--dry-run", "--owner", "cli-worker"]
            )
            self.assertEqual(code, 0)
            self.assertTrue(body["dry_run"])
            self.assertEqual(len(list(outbox.glob("*.csv"))), 1)

    def test_worker_exits_3_under_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "queue.sqlite"
            code, _body = _run(["--db", str(db), "submit", "--key", "cli-key-q", "--payload", json.dumps(helpers.slip())])
            self.assertEqual(code, 0)
            Path(str(db) + ".leaseguard").write_text("{not-json", encoding="utf-8")
            code, body = _run(
                ["--db", str(db), "--outbox", str(root / "out"), "worker", "--max-jobs", "1", "--owner", "cli-worker"]
            )
            self.assertEqual(code, 3)
            self.assertEqual(body["outcome"], "quarantine")
            self.assertFalse((root / "out").exists())
            code, body = _run(["--db", str(db), "status"])
            self.assertEqual(body["counts"]["queued"], 1)

    def test_run_lab_exports_and_dry_run_does_not(self) -> None:
        sys.path.insert(0, str(helpers.ROOT))
        import run_lab

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, body = _capture(run_lab.main, ["--state-dir", str(root / "live")])
            self.assertEqual(code, 0)
            self.assertEqual(body["succeeded"], 3)
            self.assertEqual(body["applied_intents"], 3)
            self.assertEqual(body["csv_files"], 3)
            self.assertEqual(body["fingerprint"], FINGERPRINT_ID)
            code, body = _capture(run_lab.main, ["--state-dir", str(root / "dry"), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertTrue(body["dry_run"])
            self.assertEqual(body["queued"], 3)
            self.assertEqual(body["attempts"], 0)
            self.assertEqual(body["csv_files"], 0)
            self.assertIn("HAT01", body["preview_csv"])


def _run(argv: list[str]) -> tuple[int, dict]:
    return _capture(main, argv)


def _capture(func, argv: list[str]) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = func(argv)
    text = buffer.getvalue().strip().splitlines()[-1]
    return code, json.loads(text)
