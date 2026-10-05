"""Offline demo: load the sample shift slips and export one CSV per key.

Without --state-dir the queue and the outbox live in a fresh temporary
directory. --dry-run inserts the sample into that queue and prints the CSV
the first due job would write. It does not take a lease or create a CSV file.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shiftlease.config import Config  # noqa: E402
from shiftlease.contract import FINGERPRINT_ID  # noqa: E402
from shiftlease.effect import CsvEffect  # noqa: E402
from shiftlease.logjson import JsonLogger  # noqa: E402
from shiftlease.store import QueueStore  # noqa: E402
from shiftlease.worker import Worker  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the sample shift-slip batch.")
    parser.add_argument("--state-dir", default="", help="Directory for the queue and the outbox.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch", default=str(ROOT / "examples" / "shift_batch.json"))
    args = parser.parse_args(argv)
    if args.state_dir:
        state = Path(args.state_dir)
        state.mkdir(parents=True, exist_ok=True)
        report = run(state, dry_run=args.dry_run, batch=Path(args.batch))
        _emit(report)
        return 0
    with tempfile.TemporaryDirectory(prefix="shiftlease-") as tmp:
        report = run(Path(tmp), dry_run=args.dry_run, batch=Path(args.batch))
        report["state_dir"] = tmp
        _emit(report)
    return 0


def run(state: Path, *, dry_run: bool, batch: Path) -> dict:
    payload = json.loads(batch.read_text(encoding="utf-8"))
    config = Config(
        lease_term_seconds=30,
        heartbeat_seconds=0,
        clock_uncertainty_seconds=0,
        lease_jitter_seconds=0,
        retention_seconds=7 * 24 * 60 * 60,
        synchronous="FULL",
    )
    outbox = state / "outbox"
    store = QueueStore.open(state / "queue.sqlite", config)
    try:
        for job in payload["jobs"]:
            store.submit(job["idempotency_key"], job["payload"])
        if dry_run:
            preview = store.preview_next()
            attempts = 0
            for job in payload["jobs"]:
                row = store.job_by_key(job["idempotency_key"])
                assert row is not None
                attempts += int(row["attempts"])
                if row["status"] != "queued":
                    raise RuntimeError("dry-run changed a job status")
            return {
                "dry_run": True,
                "submitted": len(payload["jobs"]),
                "queued": store.counts()["queued"],
                "attempts": attempts,
                "csv_files": len(list(outbox.glob("*.csv"))) if outbox.exists() else 0,
                "preview_job_id": None if preview is None else preview.get("job_id"),
                "preview_csv": "" if preview is None else preview.get("csv", ""),
                "fingerprint": FINGERPRINT_ID,
                "state_dir": str(state),
            }
        worker = Worker(
            store,
            CsvEffect(outbox),
            "demo-worker",
            config,
            random.Random(20261005),
            lambda _delay: None,
            JsonLogger(),
        )
        outcome = worker.run_until_idle()
        counts = store.counts()
        return {
            "dry_run": False,
            "outcome": outcome,
            "submitted": len(payload["jobs"]),
            "succeeded": counts["succeeded"],
            "applied_intents": counts["applied_intents"],
            "csv_files": len(list(outbox.glob("*.csv"))),
            "fingerprint": FINGERPRINT_ID,
            "state_dir": str(state),
        }
    finally:
        store.close()


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
