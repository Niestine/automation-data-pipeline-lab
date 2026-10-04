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

from web_collection_lab.catalog import CatalogStore  # noqa: E402
from web_collection_lab.checkpoint import MemoryCheckpointStore  # noqa: E402
from web_collection_lab.client import build_client  # noqa: E402
from web_collection_lab.collector import CollectJob  # noqa: E402
from web_collection_lab.fixture_site import FixtureSite  # noqa: E402
from web_collection_lab.retry import RetryPolicy  # noqa: E402
from web_collection_lab.schema import validate_snapshot  # noqa: E402
from web_collection_lab.seed import (  # noqa: E402
    build_catalog,
    build_previous_snapshot,
    build_robots_txt,
)
from web_collection_lab.telemetry import JsonLogger, ManualClock, RecordingSleeper  # noqa: E402


def make_job(
    *,
    catalog: Optional[list[dict[str, Any]]] = None,
    faults: Optional[list[dict[str, Any]]] = None,
    crash_after_products: Optional[int] = None,
    fail_fast: bool = False,
    force: bool = False,
    min_interval_ms: int = 0,
    burst: int = 1,
    previous: Optional[dict[str, Any]] = None,
    robots_txt: Optional[str] = None,
    page_size: int = 4,
    job_id: str = "lab-collect",
    catalog_store: Optional[CatalogStore] = None,
    store: Any = None,
):
    clock = ManualClock()
    logger = JsonLogger(clock=clock)
    sleeper = RecordingSleeper(clock)
    site = FixtureSite(
        catalog if catalog is not None else build_catalog(),
        robots_txt=robots_txt if robots_txt is not None else build_robots_txt(),
        faults=faults or [],
        page_size=page_size,
    )
    client, transport, limiter, inner = build_client(
        site,
        policy=RetryPolicy(),
        logger=logger,
        clock=clock,
        sleeper=sleeper,
        seed=7,
        min_interval_ms=min_interval_ms,
        burst=burst,
        force=force,
    )
    dest = catalog_store if catalog_store is not None else CatalogStore()
    prev = CatalogStore()
    if previous is not None:
        prev.load_from_snapshot(previous)
    else:
        parsed = validate_snapshot(build_previous_snapshot())
        prev.load_from_snapshot(
            {
                "origin": parsed["origin"],
                "collected_at": parsed["collected_at"],
                "job_id": parsed["job_id"],
                "products": [item.to_dict() for item in parsed["products"]],
                "etags": parsed["etags"],
            }
        )
    job = CollectJob(
        client,
        catalog=dest,
        previous=prev,
        store=store if store is not None else MemoryCheckpointStore(),
        logger=logger,
        clock=clock,
        job_id=job_id,
        fail_fast=fail_fast,
        crash_after_products=crash_after_products,
    )
    return job, site, sleeper, logger, clock, transport, limiter, inner
