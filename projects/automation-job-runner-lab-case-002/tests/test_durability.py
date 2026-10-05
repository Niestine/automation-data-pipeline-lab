"""Commit boundaries, process termination, and a lost WAL quarantine."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import helpers
from shiftlease.store import QueueStore


class DurabilityTests(unittest.TestCase):
    def test_uncommitted_claim_is_invisible_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            other = QueueStore.open(store.path, store.config)
            store.submit("slip-1", helpers.slip())
            claimed = store.claim("holder-a", random.Random(1), commit=False)
            self.assertEqual(claimed.kind, "job")
            visible = other.job(claimed.job_id)
            assert visible is not None
            self.assertEqual(visible["status"], "queued")
            self.assertEqual(visible["fence"], 0)
            self.assertEqual(visible["attempts"], 0)
            store.rollback()
            after = other.job(claimed.job_id)
            assert after is not None
            self.assertEqual(after["status"], "queued")
            self.assertEqual(after["fence"], 0)
            other.close()
            store.close()

    def test_kill_before_commit_leaves_the_row_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            path = store.path
            config = store.config
            store.close()
            ready = root / "ready.txt"
            err = root / "err.txt"
            proc = _spawn(["claim-open", str(path), "holder-a", str(ready)], err)
            try:
                self.assertTrue(_wait_text(ready, proc), _text(err))
                proc.kill()
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=15)
                _close_err(proc)
            reopened = _open_retry(path, config)
            row = reopened.job_by_key("slip-1")
            assert row is not None
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["fence"], 0)
            self.assertEqual(row["attempts"], 0)
            self.assertIsNone(row["owner"])
            reopened.close()

    def test_kill_after_commit_keeps_one_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            path = store.path
            config = store.config
            store.close()
            ready = root / "ready.json"
            err = root / "err.txt"
            proc = _spawn(["claim-sleep", str(path), "holder-a", str(ready)], err)
            try:
                payload = _wait_json(ready, proc)
                self.assertIsNotNone(payload, _text(err))
                proc.kill()
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=15)
                _close_err(proc)
            reopened = _open_retry(path, config)
            row = reopened.job_by_key("slip-1")
            assert row is not None and payload is not None
            self.assertEqual(row["status"], "leased")
            self.assertEqual(row["owner"], payload["owner"])
            self.assertEqual(row["fence"], payload["fence"])
            self.assertEqual(row["lease_until"], payload["lease_until"])
            self.assertEqual(row["attempts"], payload["attempts"])
            reopened.close()

    def test_clean_reopen_preserves_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            claimed = store.claim("holder-a", random.Random(1))
            path = store.path
            config = store.config
            store.close()
            reopened = QueueStore.open(path, config)
            row = reopened.job(claimed.job_id)
            assert row is not None
            self.assertEqual(row["status"], "leased")
            self.assertEqual(row["owner"], "holder-a")
            self.assertEqual(row["fence"], claimed.fence)
            self.assertEqual(row["lease_until"], claimed.lease_until)
            self.assertEqual(reopened.claim("holder-b", random.Random(2)).kind, "empty")
            reopened.close()

    def test_main_file_without_uncheckpointed_wal_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, autocheckpoint=0, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            store.checkpoint()
            claimed = store.claim("holder-a", random.Random(1))
            self.assertEqual(claimed.kind, "job")
            dest = root / "restored.sqlite"
            shutil.copyfile(store.path, dest)
            shutil.copyfile(store.guard_path, Path(str(dest) + ".leaseguard"))
            self.assertFalse((root / "restored.sqlite-wal").exists())
            store.close()
            restored = QueueStore.open(dest, store.config, autocheckpoint=0)
            self.assertTrue(restored.quarantine_active())
            blocked = restored.claim("holder-b", random.Random(2))
            self.assertEqual(blocked.kind, "quarantine")
            row = restored.job_by_key("slip-1")
            assert row is not None
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["fence"], 0)
            self.assertEqual(row["attempts"], 0)
            restored.shift_clock(restored.config.max_lease_term_seconds)
            self.assertFalse(restored.quarantine_active())
            taken = restored.claim("holder-b", random.Random(3))
            self.assertEqual(taken.kind, "job")
            restored.close()

    def test_corrupt_guard_refuses_grants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30)
            store.submit("slip-1", helpers.slip())
            path = store.path
            guard = store.guard_path
            config = store.config
            store.close()
            guard.write_text("{not-json", encoding="utf-8")
            reopened = QueueStore.open(path, config)
            self.assertTrue(reopened.quarantine_active())
            self.assertEqual(reopened.claim("holder-a", random.Random(1)).kind, "quarantine")
            self.assertEqual(reopened.job_by_key("slip-1")["status"], "queued")
            reopened.close()

    def test_status_reads_beside_an_open_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, busy_timeout_ms=1000)
            other = QueueStore.open(store.path, store.config)
            store.submit("slip-1", helpers.slip())
            store.conn.execute("BEGIN IMMEDIATE")
            try:
                report = other.status()
            finally:
                store.conn.rollback()
            self.assertEqual(report["counts"]["queued"], 1)
            self.assertIn("sha256-canonical-json-v1", report["fingerprint"])
            other.close()
            store.close()


def _close_err(proc: subprocess.Popen) -> None:
    handle = getattr(proc, "_err_handle", None)
    if handle is not None and not handle.closed:
        handle.close()


def _spawn(args: list[str], err_path: Path) -> subprocess.Popen:
    handle = err_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "shiftlease.pool", *args],
        cwd=str(helpers.ROOT),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=handle,
    )
    proc._err_handle = handle  # type: ignore[attr-defined]
    return proc


def _text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _wait_text(path: Path, proc: subprocess.Popen, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def _wait_json(path: Path, proc: subprocess.Popen, timeout: float = 20) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                return payload
        if proc.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _open_retry(path: Path, config) -> QueueStore:
    last: Exception | None = None
    for _ in range(30):
        try:
            return QueueStore.open(path, config)
        except Exception as exc:  # sqlite may still be releasing the killed process
            last = exc
            time.sleep(0.1)
    assert last is not None
    raise last
