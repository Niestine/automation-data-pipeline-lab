"""Process entry points. Children import this module, so it stays import-safe."""

from __future__ import annotations

import json
import random
import sys
import time
import traceback
from pathlib import Path


def drain_queue(db_path: str, outbox: str, owner: str) -> None:
    from shiftlease.config import Config
    from shiftlease.effect import CsvEffect
    from shiftlease.logjson import JsonLogger
    from shiftlease.store import QueueStore
    from shiftlease.worker import Worker

    config = Config(
        lease_term_seconds=60,
        heartbeat_seconds=0,
        clock_uncertainty_seconds=0,
        lease_jitter_seconds=0,
        busy_timeout_ms=5000,
        jitter_base_seconds=0.0,
        jitter_cap_seconds=0.0,
        effect_retry_cap=1,
        max_attempts=5,
        retention_seconds=86400,
        synchronous="FULL",
    )
    store = QueueStore.open(db_path, config)
    try:
        worker = Worker(
            store,
            CsvEffect(Path(outbox)),
            owner,
            config,
            random.Random(owner),
            lambda _delay: None,
            JsonLogger(),
        )
        outcome = worker.run_until_idle(max_idle=5, spin_limit=200000)
        if outcome != "drained":
            raise RuntimeError(f"worker {owner} stopped with {outcome}")
    except Exception:
        traceback.print_exc()
        raise
    finally:
        store.close()


def claim_and_sleep(db_path: str, owner: str, ready_path: str, hold_seconds: int = 30) -> None:
    """Commit one claim, then sleep so the parent can terminate this process."""
    from shiftlease.config import Config
    from shiftlease.store import QueueStore

    config = Config(
        lease_term_seconds=30,
        heartbeat_seconds=0,
        clock_uncertainty_seconds=0,
        lease_jitter_seconds=0,
        retention_seconds=86400,
    )
    store = QueueStore.open(db_path, config)
    try:
        claimed = store.claim(owner, random.Random(1))
        if claimed.kind != "job":
            raise RuntimeError(f"expected a job, got {claimed.kind}")
        Path(ready_path).write_text(
            json.dumps(
                {
                    "job_id": claimed.job_id,
                    "fence": claimed.fence,
                    "owner": claimed.owner,
                    "lease_until": claimed.lease_until,
                    "attempts": claimed.attempts,
                }
            ),
            encoding="utf-8",
        )
        time.sleep(hold_seconds)
    finally:
        store.close()


def claim_without_commit(db_path: str, owner: str, ready_path: str, hold_seconds: int = 30) -> None:
    """Leave the claim transaction open so a kill rolls it back."""
    from shiftlease.config import Config
    from shiftlease.store import QueueStore

    config = Config(
        lease_term_seconds=30,
        heartbeat_seconds=0,
        clock_uncertainty_seconds=0,
        lease_jitter_seconds=0,
        retention_seconds=86400,
    )
    store = QueueStore.open(db_path, config)
    claimed = store.claim(owner, random.Random(1), commit=False)
    if claimed.kind != "job":
        store.rollback()
        raise RuntimeError(f"expected a job, got {claimed.kind}")
    Path(ready_path).write_text(str(claimed.job_id), encoding="utf-8")
    time.sleep(hold_seconds)


def _main(argv: list[str]) -> int:
    command = argv[1]
    try:
        if command == "drain":
            drain_queue(argv[2], argv[3], argv[4])
        elif command == "claim-sleep":
            claim_and_sleep(argv[2], argv[3], argv[4])
        elif command == "claim-open":
            claim_without_commit(argv[2], argv[3], argv[4])
        else:
            raise SystemExit(f"unknown pool command {command}")
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
