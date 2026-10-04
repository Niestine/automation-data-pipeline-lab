"""Path bootstrap and factories for unittest modules."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from maintenance_lab.apply import MaintenancePipeline  # noqa: E402
from maintenance_lab.catalog import CatalogStore  # noqa: E402
from maintenance_lab.checkpoint import MemoryCheckpointStore  # noqa: E402
from maintenance_lab.ledger import FeedLedger  # noqa: E402
from maintenance_lab.models import LAB_NOW_MS, FeedInput, Product  # noqa: E402
from maintenance_lab.seed import build_products  # noqa: E402
from maintenance_lab.telemetry import JsonLogger, ManualClock  # noqa: E402


def make_pipeline(
    *,
    products: Optional[list[Product]] = None,
    now_ms: int = LAB_NOW_MS,
    crash_after_rows: Optional[int] = None,
    fail_fast: bool = False,
    force: bool = False,
    catalog: Optional[CatalogStore] = None,
    ledger: Optional[FeedLedger] = None,
    checkpoints: Any = None,
):
    clock = ManualClock(now_ms)
    logger = JsonLogger(clock=clock)
    store = catalog if catalog is not None else CatalogStore(products=products if products is not None else build_products())
    feed_ledger = ledger if ledger is not None else FeedLedger()
    ckpt = checkpoints if checkpoints is not None else MemoryCheckpointStore()
    pipeline = MaintenancePipeline(
        store,
        ledger=feed_ledger,
        checkpoints=ckpt,
        logger=logger,
        clock=clock,
        crash_after_rows=crash_after_rows,
        fail_fast=fail_fast,
        force=force,
    )
    return pipeline, store, logger, clock, feed_ledger, ckpt


def feed(name: str, text: str, encoding: str = "utf-8") -> FeedInput:
    return FeedInput(name=name, data=text.encode(encoding))
