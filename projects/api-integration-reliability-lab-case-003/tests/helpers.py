"""Shared fixtures for the plot-allotment lab."""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from plot_allotment.client import ExportClient  # noqa: E402
from plot_allotment.clock import ManualClock  # noqa: E402
from plot_allotment.httpmsg import Request  # noqa: E402
from plot_allotment.journal import Journal  # noqa: E402
from plot_allotment.schema import canonical_bytes  # noqa: E402
from plot_allotment.service import Service  # noqa: E402
from plot_allotment.store import Store  # noqa: E402
from plot_allotment.worker import ExportWorker  # noqa: E402

PARENT = "garden-north"
CALLER = "desk-a"


def lot(resource_id: str, sort_key: int, note: str = "open", version: int = 1) -> dict:
    suffix = resource_id.replace("lot-", "")
    return {
        "beds": 1,
        "holder_label": f"holder-{suffix}",
        "note": note,
        "plot_code": f"N-{suffix[-2:]}",
        "resource_id": resource_id,
        "sort_key": sort_key,
        "source_version": version,
    }


class Lab:
    def __init__(self, seed: int = 1, now: float = 1_700_000_000.0) -> None:
        self.clock = ManualClock(now)
        self.rng = random.Random(seed)
        self.journal = Journal()
        self.sleeps: list[float] = []
        self.store = Store(":memory:", self.journal)
        self.service = Service(self.store, self.clock, self.journal, self.rng)
        self.client = ExportClient(
            self.service,
            CALLER,
            sleeper=self.sleeper,
            rng=self.rng,
            journal=self.journal,
        )
        self.worker = ExportWorker(
            self.service,
            self.store,
            self.client,
            self.journal,
            self.clock,
            owner="worker-a",
        )

    def sleeper(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def add(self, *rows: dict) -> None:
        for row in rows:
            self.store.insert_source(PARENT, row)

    def close(self) -> None:
        self.store.close()


def bearer(caller: str = CALLER) -> dict[str, str]:
    return {"authorization": f"Bearer {caller}"}


def upsert_request(body: dict, key: str | None, caller: str = CALLER, if_match: str | None = None) -> Request:
    headers = {**bearer(caller), "content-type": "application/json"}
    if key is not None:
        headers["idempotency-key"] = key
    if if_match is not None:
        headers["if-match"] = if_match
    return Request(
        method="POST",
        path=f"/v1/{PARENT}/resources:upsert",
        headers=headers,
        body=canonical_bytes(body),
    )


__all__ = [
    "CALLER",
    "PARENT",
    "Lab",
    "Request",
    "bearer",
    "canonical_bytes",
    "lot",
    "upsert_request",
]
