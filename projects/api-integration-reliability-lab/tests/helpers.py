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

from api_reliability_lab.checkpoint import MemoryCheckpointStore  # noqa: E402
from api_reliability_lab.client import build_client  # noqa: E402
from api_reliability_lab.ledger import Ledger  # noqa: E402
from api_reliability_lab.mock_service import MockFulfillmentApi  # noqa: E402
from api_reliability_lab.models import LAB_TOKEN, LAB_WEBHOOK_SECRET  # noqa: E402
from api_reliability_lab.retry import RetryPolicy  # noqa: E402
from api_reliability_lab.seed import build_catalog  # noqa: E402
from api_reliability_lab.sync import SyncJob  # noqa: E402
from api_reliability_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper  # noqa: E402
from api_reliability_lab.webhooks import WebhookReceiver, build_delivery  # noqa: E402


def make_job(
    *,
    catalog: Optional[list[dict[str, Any]]] = None,
    faults: Optional[list[dict[str, Any]]] = None,
    crash_after_pages: Optional[int] = None,
    crash_at: str = "post_upsert",
    fail_fast: bool = False,
    page_limit: int = 10,
    policy: Optional[RetryPolicy] = None,
    ledger: Optional[Ledger] = None,
    store: Any = None,
):
    clock = ManualClock()
    logger = JsonLogger()
    sleeper = RecordingSleeper(clock)
    catalog = catalog if catalog is not None else build_catalog()
    service = MockFulfillmentApi(catalog, token=LAB_TOKEN, faults=faults or [])
    client, transport = build_client(
        service,
        token=LAB_TOKEN,
        policy=policy if policy is not None else RetryPolicy(),
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        seed=7,
        default_limit=page_limit,
    )
    ledger = ledger if ledger is not None else Ledger()
    store = store if store is not None else MemoryCheckpointStore()
    receiver = WebhookReceiver(
        ledger, secret=LAB_WEBHOOK_SECRET, clock=clock, logger=logger
    )
    job = SyncJob(
        client,
        ledger=ledger,
        store=store,
        receiver=receiver,
        logger=logger,
        clock=clock,
        crash_after_pages=crash_after_pages,
        crash_at=crash_at,
        fail_fast=fail_fast,
        page_limit=page_limit,
    )
    return job, service, sleeper, logger, clock, transport


def signed_deliveries(events, clock, *, secret: str = LAB_WEBHOOK_SECRET, timestamp: Optional[str] = None):
    ts = timestamp if timestamp is not None else str(clock.now_s())
    return [build_delivery(event, secret=secret, timestamp=ts) for event in events]
