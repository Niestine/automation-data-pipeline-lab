"""Path bootstrap and queue factories. Synthetic shift slips only."""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
EXAMPLES = ROOT / "examples"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_parts = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
if str(SRC) not in _parts:
    os.environ["PYTHONPATH"] = os.pathsep.join([str(SRC), *_parts]) if _parts else str(SRC)

from shiftlease.config import Config  # noqa: E402
from shiftlease.effect import CsvEffect  # noqa: E402
from shiftlease.logjson import JsonLogger  # noqa: E402
from shiftlease.store import QueueStore  # noqa: E402
from shiftlease.worker import Worker  # noqa: E402


def lab_config(**overrides: object) -> Config:
    values: dict = {
        "lease_term_seconds": 4,
        "heartbeat_seconds": 0,
        "clock_uncertainty_seconds": 0,
        "lease_jitter_seconds": 0,
        "busy_timeout_ms": 2000,
        "jitter_base_seconds": 0.0,
        "jitter_cap_seconds": 0.0,
        "effect_retry_cap": 2,
        "max_attempts": 5,
        "retention_seconds": 3600,
        "synchronous": "FULL",
    }
    values.update(overrides)
    return Config(**values)


def slip(desk: str = "lane-a", sku: str = "HAT01", qty: int = 2, bin_code: str = "A01") -> dict:
    return {"desk": desk, "rows": [{"sku": sku, "qty": qty, "bin": bin_code}]}


def open_store(directory: Path, name: str = "queue.sqlite", autocheckpoint: int = 1000, **overrides: object) -> QueueStore:
    return QueueStore.open(Path(directory) / name, lab_config(**overrides), autocheckpoint=autocheckpoint)


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_worker(store: QueueStore, outbox: Path, owner: str = "holder-a", seed: int = 1, faults=None) -> tuple[Worker, CsvEffect, JsonLogger, RecordingSleeper]:
    effect = CsvEffect(outbox)
    logger = JsonLogger()
    sleeper = RecordingSleeper()
    worker = Worker(
        store,
        effect,
        owner,
        store.config,
        random.Random(seed),
        sleeper,
        logger,
        faults,
    )
    return worker, effect, logger, sleeper
