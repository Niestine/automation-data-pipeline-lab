"""Offline demo. Prints one JSON object and does not open a socket."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lotcycle.auth_server import RegistrationError
from lotcycle.errors import ReauthRequired
from lotcycle.httputil import Request
from lotcycle.params import ACCESS_LIFETIME
from lotcycle.schema import canonical_json
from lotcycle.webhooks import endpoint_secret
from lotcycle.world import install_feed, install_grants, load_examples, open_lab

ENDPOINT = "desk-north"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_lab.py")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--drop-refresh", action="store_true")
    parser.add_argument("--state", default=None, help="Directory for lotcycle.sqlite")
    args = parser.parse_args(argv)
    try:
        report = execute(args)
    except (OSError, RegistrationError, ValueError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


def execute(args: argparse.Namespace) -> dict:
    examples = load_examples()
    if args.dry_run:
        lab = open_lab()
    else:
        database = ":memory:"
        if args.state:
            folder = Path(args.state)
            folder.mkdir(parents=True, exist_ok=True)
            database = str(folder / "lotcycle.sqlite")
        lab = open_lab(database)
    try:
        install_grants(lab, examples["grants"])
        install_feed(lab, examples["excursions"])
        client = lab.clients["handheld-north"]
        if args.dry_run:
            entries = client.sync(commit=False)
            return _report(lab, client, entries=len(entries), dry_run=True)
        fault = examples["fault"]
        if args.drop_refresh:
            lab.transport.drop_after_send = 1
        else:
            lab.auth.fail_before_commit = int(fault["precommit_failures"])
            if fault.get("drop_after_commit"):
                lab.transport.drop_after_send = 1
        lab.clock.advance(ACCESS_LIFETIME)
        try:
            client.ensure_access()
        except ReauthRequired:
            if not args.drop_refresh and not fault.get("drop_after_commit"):
                raise
            return _report(lab, client, entries=0, dry_run=False)
        entries = client.sync(commit=True)
        order = client.post_order({"qty": 2, "sku": "crate-ice"}, "restock-north-001")
        if order.status != 201:
            raise ValueError("order status " + str(order.status))
        webhook_status, found = _deliver(lab, examples["event"])
        report = _report(lab, client, entries=len(entries), dry_run=False)
        report["webhook_resource_found"] = found
        report["webhook_status"] = webhook_status
        return report
    finally:
        lab.close()


def _deliver(lab, event: dict) -> tuple[int, bool]:
    secret = endpoint_secret(ENDPOINT)
    lab.hooks.trust(ENDPOINT, [secret])

    def reader(resource_id: str) -> bool:
        return lab.resources["lot-ledger"].find_entry("handheld-north", resource_id) is not None

    lab.hooks.reader = reader
    body = canonical_json(event)

    def receiver(attempt: dict):
        return lab.hooks.handle(
            Request(
                "POST",
                "/hooks/" + ENDPOINT,
                attempt["headers"],
                attempt["body"],
            )
        )

    from lotcycle.webhooks import WebhookProducer

    producer = WebhookProducer(
        lab.clock,
        lab.rng,
        ENDPOINT,
        secret,
        "https://handheld.coldlot.example/hooks/" + ENDPOINT,
    )
    result = producer.deliver("evt-excursion-120", body, receiver)
    found = bool(lab.hooks.events and lab.hooks.events[-1]["resource_found"])
    return int(result["status"]), found


def _report(lab, client, entries: int, dry_run: bool) -> dict:
    family = lab.store.get_family(client.family_id)
    posts = sum(1 for path in lab.transport.sent if path == "/token")
    return {
        "checkpoints": lab.store.checkpoint_count(client.client_id, client.audience),
        "client_id": client.client_id,
        "client_status": client.status,
        "decisions": list(client.decisions),
        "dry_run": dry_run,
        "entries": entries,
        "orders": lab.store.order_count(client.client_id),
        "reconstruction_incomplete": client.reconstruction_incomplete,
        "refresh_posts": posts,
        "server_generation": None if family is None else family["active_generation"],
        "server_status": None if family is None else family["status"],
        "webhook_resource_found": None,
        "webhook_status": None,
    }
