"""One live owner per job, and a wedged effect does not block the next claim."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import helpers
from shiftlease.effect import CsvEffect
from shiftlease.store import QueueStore


class ConcurrencyTests(unittest.TestCase):
    def test_second_job_is_claimed_while_the_first_effect_is_still_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=30, busy_timeout_ms=1000)
            store.submit("slip-1", helpers.slip())
            store.submit("slip-2", helpers.slip(sku="COAT7", qty=1, bin_code="B12"))
            started = threading.Event()
            release = threading.Event()
            inner = CsvEffect(root / "out")

            class Gate:
                def perform(self, **kwargs: object) -> dict:
                    started.set()
                    if not release.wait(3):
                        raise TimeoutError("effect was not released")
                    return inner.perform(**kwargs)

            from shiftlease.logjson import JsonLogger
            from shiftlease.worker import Worker

            worker = Worker(
                store,
                Gate(),  # type: ignore[arg-type]
                "holder-a",
                store.config,
                random.Random(1),
                lambda _delay: None,
                JsonLogger(),
            )
            thread = threading.Thread(target=worker.run_once)
            thread.start()
            self.assertTrue(started.wait(3))
            other = QueueStore.open(store.path, store.config)
            try:
                second = other.claim("holder-b", random.Random(2))
            finally:
                release.set()
                other.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(second.kind, "job")
            self.assertEqual(second.idempotency_key, "slip-2")
            first = store.job_by_key("slip-1")
            assert first is not None
            self.assertEqual(first["status"], "succeeded")
            self.assertEqual(first["owner"], "holder-a")
            self.assertNotEqual(second.job_id, first["id"])
            store.close()

    def test_eight_processes_finish_one_hundred_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = helpers.open_store(root, lease_term_seconds=60, busy_timeout_ms=5000)
            for index in range(100):
                store.submit(
                    f"job-{index:03d}",
                    {"desk": "lane-a", "rows": [{"sku": f"S{index:04d}", "qty": 1, "bin": "A01"}]},
                )
            db = store.path
            store.close()
            outbox = root / "out"
            procs: list[tuple[subprocess.Popen, Path, object]] = []
            try:
                for index in range(8):
                    err = root / f"worker-{index}.err"
                    handle = err.open("w", encoding="utf-8")
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "shiftlease.pool", "drain", str(db), str(outbox), f"proc-{index}"],
                        cwd=str(helpers.ROOT),
                        env=os.environ.copy(),
                        stdout=subprocess.DEVNULL,
                        stderr=handle,
                    )
                    procs.append((proc, err, handle))
                codes = []
                for proc, err, handle in procs:
                    code = proc.wait(timeout=120)
                    handle.close()
                    detail = err.read_text(encoding="utf-8", errors="replace") if code else ""
                    codes.append((code, detail))
            finally:
                for proc, _err, handle in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=15)
                    handle.close()
            failed = [item for item in codes if item[0] != 0]
            self.assertEqual(failed, [])
            checked = helpers.open_store(root, lease_term_seconds=60, busy_timeout_ms=5000)
            counts = checked.counts()
            self.assertEqual(counts["succeeded"], 100)
            self.assertEqual(counts["queued"], 0)
            self.assertEqual(counts["leased"], 0)
            self.assertEqual(counts["dead"], 0)
            self.assertEqual(counts["applied_intents"], 100)
            self.assertEqual(counts["pending_intents"], 0)
            files = list(outbox.glob("*.csv"))
            self.assertEqual(len(files), 100)
            seen: set[str] = set()
            for path in files:
                key = path.read_text(encoding="utf-8").splitlines()[1].split(",")[0]
                seen.add(key)
            self.assertEqual(len(seen), 100)
            for index in range(100):
                row = checked.job_by_key(f"job-{index:03d}")
                assert row is not None
                self.assertEqual(row["status"], "succeeded")
                self.assertEqual(row["fence"], 1)
                events = checked.events(row["id"])
                complete = [item for item in events if item["kind"] == "complete"]
                claim = [item for item in events if item["kind"] == "claim"]
                self.assertEqual(len(complete), 1)
                self.assertEqual(len(claim), 1)
                self.assertEqual(complete[0]["fence"], claim[0]["fence"])
                self.assertEqual(complete[0]["fence"], row["fence"])
                self.assertLess(claim[0]["seq"], complete[0]["seq"])
            checked.close()
