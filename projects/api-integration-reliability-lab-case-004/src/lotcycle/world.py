"""Seed the in-process cold-lot office from the example documents."""

from __future__ import annotations

import json
import random
from pathlib import Path

from lotcycle.auth_server import AuthServer
from lotcycle.client import GrantClient
from lotcycle.clock import VirtualClock
from lotcycle.httputil import Transport
from lotcycle.params import ISSUER, PAGE_SIZE
from lotcycle.resource_server import ResourceServer
from lotcycle.store import Store
from lotcycle.webhooks import WebhookConsumer


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_examples(root: Path | None = None) -> dict:
    root = root or project_root()
    examples = root / "examples"
    return {
        "event": _read(examples / "webhook_event.json"),
        "excursions": _read(examples / "excursions.json"),
        "fault": _read(examples / "fault_script.json"),
        "grants": _read(examples / "grants.json"),
    }


def _read(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(path.name + " must be a JSON object")
    return document


def make_entries(spec: dict) -> list[dict]:
    count = int(spec["count"])
    if count < 1:
        raise ValueError("entry count")
    rows = []
    for index in range(1, count + 1):
        rows.append(
            {
                "celsius": -18 - (index % 5),
                "id": f"{spec['id_prefix']}-{index:03d}",
                "lot": f"{spec['lot_prefix']}-{index:03d}",
                "updated": index,
            }
        )
    sample = spec.get("samples", [None])[0]
    if sample is not None and rows[0] != sample:
        raise ValueError("sample entry does not match the generator")
    return rows


class Lab:
    def __init__(
        self,
        clock: VirtualClock,
        rng: random.Random,
        store: Store,
        auth: AuthServer,
        resources: dict[str, ResourceServer],
        hooks: WebhookConsumer,
        transport: Transport,
        seed: int,
    ) -> None:
        self.clock = clock
        self.rng = rng
        self.store = store
        self.auth = auth
        self.resources = resources
        self.hooks = hooks
        self.transport = transport
        self.seed = seed
        self.clients: dict[str, GrantClient] = {}

    def close(self) -> None:
        self.store.close()


def open_lab(path: str = ":memory:", seed: int = 7, start: float = 1_700_000_000) -> Lab:
    clock = VirtualClock(start)
    rng = random.Random(seed)
    store = Store(path, clock)
    auth = AuthServer(store, clock)
    resources = {
        "alarm-board": ResourceServer(store, clock, "alarm-board"),
        "lot-ledger": ResourceServer(store, clock, "lot-ledger"),
    }
    hooks = WebhookConsumer(store, clock)
    transport = Transport(
        handlers={
            "alarm-board": resources["alarm-board"].handle,
            "hooks": hooks.handle,
            "lot-ledger": resources["lot-ledger"].handle,
            "token": auth.handle,
        }
    )
    return Lab(clock, rng, store, auth, resources, hooks, transport, seed)


def install_grants(lab: Lab, grants: dict) -> None:
    if grants.get("issuer") != ISSUER:
        raise ValueError("issuer does not match the lab authorization server")
    clients = grants.get("clients")
    if not isinstance(clients, list) or not clients:
        raise ValueError("clients")
    for index, spec in enumerate(clients):
        if not isinstance(spec, dict):
            raise ValueError("client spec")
        lab.auth.register_client(spec)
        issued = lab.auth.issue_family(spec["client_id"])
        row = lab.store.get_client(spec["client_id"])
        if row is None:
            raise ValueError(spec["client_id"])
        lab.clients[spec["client_id"]] = GrantClient(
            access_exp=float(issued["expires_at"]),
            access_token=issued["access_token"],
            audience=spec["audience"],
            client_id=spec["client_id"],
            client_type=spec["client_type"],
            clock=lab.clock,
            family_id=issued["family_id"],
            mode=row["mode"],
            refresh_token=issued["refresh_token"],
            rng=random.Random(lab.seed + index + 1),
            secret=spec.get("secret"),
            sender_key_id=row["sender_key_id"],
            store=lab.store,
            transport=lab.transport,
        )


def install_feed(lab: Lab, excursions: dict) -> None:
    ledger = excursions["ledger"]
    if int(ledger["page_size"]) != PAGE_SIZE:
        raise ValueError("page_size")
    north = make_entries(ledger)
    if ledger["client_id"] not in lab.clients:
        raise ValueError("ledger client")
    lab.resources["lot-ledger"].load_entries(ledger["client_id"], north)
    alarms = excursions["alarms"]
    lab.resources["alarm-board"].load_entries(alarms["client_id"], make_entries(alarms))
