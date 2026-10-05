"""Path bootstrap and request factories."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

EXAMPLES = ROOT / "examples"

from quay_inbox.clock import ManualClock  # noqa: E402
from quay_inbox.httpmsg import HttpRequest  # noqa: E402
from quay_inbox.mac import sign_standard, sign_stripe  # noqa: E402
from quay_inbox.secrets import ROUTE_A, Route  # noqa: E402
from quay_inbox.store import Store  # noqa: E402


def standard_body(
    event_type: str = "release.granted",
    object_id: str = "rel_1001",
    status: str = "granted",
    berth: str = "Q3",
    api_version: str = "2024-09-01",
    created: int | None = 1_700_000_000,
    yard_code: str | None = None,
) -> bytes:
    data = {"berth": berth, "id": object_id, "status": status}
    if yard_code is not None:
        data["yard_code"] = yard_code
    payload = {"api_version": api_version, "data": data, "type": event_type}
    if created is not None:
        payload["created"] = created
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def stripe_body(
    event_id: str,
    event_type: str,
    obj: dict,
    created: int,
    api_version: str = "2024-09-01",
) -> bytes:
    payload = {
        "api_version": api_version,
        "created": created,
        "data": {"object": obj},
        "id": event_id,
        "object": "event",
        "type": event_type,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def signed_request(
    body: bytes,
    secret: bytes = ROUTE_A,
    event_id: str = "msg_quay_0001",
    timestamp: int = 1_700_000_100,
    path: str = "/hooks/quay",
    authority: str = "hooks.quay.example",
) -> HttpRequest:
    headers = {
        "content-type": "application/json",
        "webhook-id": event_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign_standard(secret, event_id, timestamp, body),
    }
    return HttpRequest("POST", path, authority, headers, body)


def stripe_request(
    body: bytes,
    secret: str,
    timestamp: int,
    path: str = "/hooks/stripe",
    *,
    include_v0: bool = False,
    authority: str = "hooks.quay.example",
) -> HttpRequest:
    headers = {
        "content-type": "application/json",
        "stripe-signature": sign_stripe(secret, timestamp, body, include_v0=include_v0),
    }
    return HttpRequest("POST", path, authority, headers, body)


def make_store_clock(start: int = 1_700_000_100):
    return Store(), ManualClock(start)


def standard_route(secret: bytes = ROUTE_A, path: str = "/hooks/quay", route_id: str = "quay") -> Route:
    return Route(route_id, path, "standard", secrets=(secret,))
