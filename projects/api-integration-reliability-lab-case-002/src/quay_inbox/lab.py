"""Wire the quay inbox demo. The transport is in-process and offline."""

from __future__ import annotations

import json
import random
from pathlib import Path

from .catalog import Checkpoint, EventCatalog, Reconciler
from .clock import ManualClock
from .httpmsg import HttpResponse
from .idempotency import IdempotencyResource
from .publisher import Publisher
from .receiver import Receiver
from .schema import notice_from_standard
from .secrets import Route
from .ssrf import ScriptedResolver
from .store import Store
from .worker import Downstream, Upstream, Worker

CALLBACK = "https://hooks.quay.example/hooks/quay"
PUBLIC_ADDRESS = "203.0.113.10"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def body_for(item: dict) -> bytes:
    payload = {
        "api_version": item["api_version"],
        "created": item["created"],
        "data": item["data"],
        "type": item["type"],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def notice_for(item: dict):
    return notice_from_standard(item["event_id"], json.loads(body_for(item)))


def load_examples(directory: Path) -> tuple[dict, dict, dict]:
    directory = Path(directory)
    routes = json.loads((directory / "routes.json").read_text(encoding="utf-8"))
    notices = json.loads((directory / "notices.json").read_text(encoding="utf-8"))
    faults = json.loads((directory / "fault_script.json").read_text(encoding="utf-8"))
    if "quay" not in routes or "push" not in notices or "missed" not in notices:
        raise ValueError("example files are missing quay, push, or missed")
    if not isinstance(routes["quay"].get("key_text"), str):
        raise ValueError("routes.json quay.key_text must be a string")
    if not isinstance(faults.get("statuses"), list) or not all(isinstance(code, int) for code in faults["statuses"]):
        raise ValueError("fault_script statuses must be a list of integers")
    return routes, notices, faults


class FaultTransport:
    def __init__(self, statuses: list[int], inner) -> None:
        self.statuses = list(statuses)
        self.inner = inner

    def __call__(self, request):
        if self.statuses:
            code = self.statuses.pop(0)
            return HttpResponse(code, b"{}", {})
        return self.inner(request)


def run_demo(
    examples: Path,
    *,
    dry_run: bool = False,
    state: Path | None = None,
    seed: int = 7,
    clock: ManualClock | None = None,
) -> dict:
    routes, notices, faults = load_examples(examples)
    clock = clock or ManualClock(1_700_000_100)
    # A synthetic 32-byte lab string. Route rejects keys outside 32-64 bytes.
    secret = routes["quay"]["key_text"].encode("utf-8")
    store = Store()
    route = Route("quay", "/hooks/quay", "standard", secrets=(secret,))
    receiver = Receiver(store, clock, [route], dry_run=dry_run)
    worker = Worker(store, Upstream({}), Downstream())

    def transport(request):
        return receiver.handle(request)

    resolver = ScriptedResolver({"hooks.quay.example": [PUBLIC_ADDRESS]})
    publisher = Publisher.register(
        CALLBACK,
        resolver,
        clock=clock,
        rng=random.Random(seed),
        transport=FaultTransport(faults["statuses"], transport),
        profile="standard",
        secrets=[secret],
        jitter_base=8,
        jitter_cap=64,
    )
    for item in notices["push"]:
        publisher.enqueue(item["event_id"], body_for(item))
    publisher.pump()
    if publisher.rows:
        pending = [row.delays[-1] for row in publisher.rows.values() if row.automatic_open and row.delays]
        if pending:
            clock.advance(max(pending))
            publisher.pump()
    worker.drain()

    catalog_events = []
    for item in notices["missed"]:
        catalog_events.append(
            {
                "id": item["event_id"],
                "created": item["created"],
                "type": item["type"],
                "delivery_success": False,
                "notice": notice_for(item),
            }
        )
    catalog = EventCatalog(catalog_events, clock)
    idempotency = IdempotencyResource(
        clock,
        lambda method, path, body, client_id: (201, body),
    )
    checkpoint = Checkpoint()
    reconciler = Reconciler(catalog, receiver, checkpoint, idempotency=idempotency, limit=10)
    reconciled = reconciler.run()
    worker.drain()
    if state is not None:
        store.save(state)
        checkpoint.save(Path(state).with_suffix(".checkpoint.json"))
    delays = []
    for row in publisher.rows.values():
        delays.extend(row.delays)
    return {
        "dry_run": dry_run,
        "accepted": receiver.accepted,
        "duplicates": receiver.duplicates,
        "inbox": len(store.inbox),
        "effects": store.memo_misses,
        "release_changes": store.release_changes,
        "reconciled_ids": reconciled,
        "acks": idempotency.handler_runs,
        "throttle_delays": delays,
        "pinned_address": publisher.target.address,
        "host": publisher.target.host,
        "sni": publisher.target.sni,
        "connections": len(publisher.connections),
        "resolver_lookups": len(resolver.calls),
        "gate_effects": len(worker.downstream.applied),
    }
