"""Standard Webhooks signatures and a compressed delivery schedule.

The MAC input is the raw body. A retry keeps webhook-id and replaces the
timestamp, which changes the signature. Only 2xx is success. Redirects
are not followed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import random
import threading

from lotcycle.clock import VirtualClock
from lotcycle.errors import SchemaError
from lotcycle.httputil import Request, Response
from lotcycle.params import (
    DELIVERY_ATTEMPTS,
    RECEIVER_TIMEOUT,
    WEBHOOK_PEPPER,
    WEBHOOK_RETENTION,
    WEBHOOK_TOLERANCE,
)
from lotcycle.retry import full_jitter
from lotcycle.schema import problem, validate_webhook_event
from lotcycle.store import Store

_BODY_LIMIT = 20 * 1024


def endpoint_secret(endpoint_id: str) -> bytes:
    return hmac.new(WEBHOOK_PEPPER, endpoint_id.encode("utf-8"), hashlib.sha256).digest()


def encode_whsec(secret: bytes) -> str:
    if not 32 <= len(secret) <= 64:
        raise ValueError("webhook secret length")
    return "whsec_" + base64.b64encode(secret).decode("ascii")


def decode_whsec(value: str) -> bytes:
    if not value.startswith("whsec_"):
        raise ValueError("whsec prefix")
    try:
        raw = base64.b64decode(value[len("whsec_") :], validate=True)
    except ValueError as exc:
        raise ValueError("whsec encoding") from exc
    if not 32 <= len(raw) <= 64:
        raise ValueError("webhook secret length")
    return raw


def mac_bytes(secret: bytes, webhook_id: str, timestamp: str, body: bytes) -> bytes:
    prefix = f"{webhook_id}.{timestamp}.".encode("utf-8")
    return hmac.new(secret, prefix + body, hashlib.sha256).digest()


def sign(secret: bytes, webhook_id: str, timestamp: str, body: bytes) -> str:
    digest = mac_bytes(secret, webhook_id, timestamp, body)
    return "v1," + base64.b64encode(digest).decode("ascii")


def signature_header(secrets: list[bytes], webhook_id: str, timestamp: str, body: bytes) -> str:
    return " ".join(sign(secret, webhook_id, timestamp, body) for secret in secrets)


def signature_ok(secrets: list[bytes], header: str, webhook_id: str, timestamp: str, body: bytes) -> bool:
    expected = [mac_bytes(secret, webhook_id, timestamp, body) for secret in secrets]
    for part in header.split():
        version, separator, encoded = part.partition(",")
        if separator != "," or version != "v1" or not encoded:
            continue
        try:
            presented = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        for candidate in expected:
            if len(presented) == len(candidate) and hmac.compare_digest(presented, candidate):
                return True
    return False


class WebhookConsumer:
    def __init__(self, store: Store, clock: VirtualClock) -> None:
        self.store = store
        self.clock = clock
        self.trusted: dict[str, list[bytes]] = {}
        self.reader = None
        self.handler_runs = 0
        self.events: list[dict] = []

    def trust(self, endpoint_id: str, secrets: list[bytes]) -> None:
        self.trusted[endpoint_id] = list(secrets)

    def handle(self, request: Request) -> Response:
        parts = [part for part in request.path.split("/") if part]
        if request.method != "POST" or len(parts) != 2 or parts[0] != "hooks":
            return _problem(404, "Not Found", "hook route")
        endpoint_id = parts[1]
        if len(request.body) > _BODY_LIMIT:
            return _problem(413, "Payload Too Large", "webhook body")
        webhook_id = request.headers.get("webhook-id")
        timestamp = request.headers.get("webhook-timestamp")
        signature = request.headers.get("webhook-signature")
        if not webhook_id or not timestamp or not signature:
            return _problem(400, "Bad Request", "webhook headers")
        if "." in webhook_id or "." in timestamp:
            return _problem(400, "Bad Request", "webhook id")
        if not timestamp.isdigit():
            return _problem(400, "Bad Request", "webhook timestamp")
        if abs(self.clock.now() - int(timestamp)) > WEBHOOK_TOLERANCE:
            return _json_status(401)
        secrets = self.trusted.get(endpoint_id)
        if not secrets or not signature_ok(secrets, signature, webhook_id, timestamp, request.body):
            return _json_status(401)
        try:
            event = validate_webhook_event(json.loads(request.body.decode("utf-8")))
        except (SchemaError, json.JSONDecodeError, UnicodeError):
            return _problem(400, "Bad Request", "webhook schema")
        now = self.clock.now()
        found = None
        duplicate = False
        with self.store.transaction():
            row = self.store.con.execute(
                """
                SELECT received_at FROM webhook_seen
                WHERE endpoint_id = ? AND webhook_id = ?
                """,
                (endpoint_id, webhook_id),
            ).fetchone()
            if row is not None and now - float(row["received_at"]) <= WEBHOOK_RETENTION:
                duplicate = True
            else:
                if row is not None:
                    self.store.con.execute(
                        "DELETE FROM webhook_seen WHERE endpoint_id = ? AND webhook_id = ?",
                        (endpoint_id, webhook_id),
                    )
                if self.reader is not None:
                    found = bool(self.reader(event["resource_id"]))
                self.store.con.execute(
                    """
                    INSERT INTO webhook_seen (endpoint_id, webhook_id, received_at, resource_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (endpoint_id, webhook_id, now, event["resource_id"]),
                )
        if duplicate:
            return Response(204, {}, b"")
        self.handler_runs += 1
        self.events.append(
            {
                "endpoint_id": endpoint_id,
                "resource_found": found,
                "resource_id": event["resource_id"],
                "type": event["type"],
                "webhook_id": webhook_id,
            }
        )
        return Response(204, {}, b"")


class WebhookProducer:
    """Five-attempt schedule on the virtual clock. The spec's ladder is multi-day."""

    def __init__(
        self,
        clock: VirtualClock,
        rng: random.Random,
        endpoint_id: str,
        secret: bytes,
        url: str,
        max_in_flight: int = 1,
    ) -> None:
        self.clock = clock
        self.rng = rng
        self.endpoint_id = endpoint_id
        self.secret = secret
        self.url = url
        self.max_in_flight = max_in_flight
        self.in_flight = 0
        self._slots = threading.Lock()
        self.max_seen = 0
        self.capped = 0
        self.disabled = False
        self.redirects: list[str] = []
        self.calls: list[str] = []

    def deliver(
        self,
        webhook_id: str,
        body: bytes,
        receiver,
        secrets: list[bytes] | None = None,
    ) -> dict:
        if self.disabled:
            return {"attempts": 0, "status": "disabled", "urls": []}
        with self._slots:
            if self.in_flight >= self.max_in_flight:
                self.capped += 1
                return {
                    "attempts": 0,
                    "in_flight": self.in_flight,
                    "status": "capped",
                    "urls": [],
                }
            self.in_flight += 1
            self.max_seen = max(self.max_seen, self.in_flight)
        try:
            return self._run(webhook_id, body, receiver, secrets or [self.secret])
        finally:
            with self._slots:
                self.in_flight -= 1

    def _run(self, webhook_id: str, body: bytes, receiver, secrets: list[bytes]) -> dict:
        attempts = 0
        urls: list[str] = []
        final = 0
        while attempts < DELIVERY_ATTEMPTS and not self.disabled:
            timestamp = str(int(self.clock.now()))
            attempt = {
                "body": body,
                "headers": {
                    "content-type": "application/json",
                    "webhook-id": webhook_id,
                    "webhook-signature": signature_header(secrets, webhook_id, timestamp, body),
                    "webhook-timestamp": timestamp,
                },
                "timeout": RECEIVER_TIMEOUT,
                "url": self.url,
            }
            attempts += 1
            self.calls.append(self.url)
            result = receiver(attempt)
            urls.append(self.url)
            status = int(getattr(result, "status", 0))
            headers = {key.lower(): value for key, value in getattr(result, "headers", {}).items()}
            timed_out = bool(getattr(result, "timed_out", False))
            final = status
            if timed_out:
                final = 0
            if 200 <= status < 300 and not timed_out:
                return {"attempts": attempts, "status": status, "urls": urls}
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location")
                if location:
                    self.redirects.append(location)
            if status == 410:
                self.disabled = True
                return {"attempts": attempts, "status": 410, "urls": urls}
            if attempts >= DELIVERY_ATTEMPTS:
                break
            delay = _next_delay(self.rng, attempts - 1, status, headers)
            self.clock.sleep(delay)
        return {"attempts": attempts, "status": final, "urls": urls}


def _next_delay(rng: random.Random, attempt: int, status: int, headers: dict[str, str]) -> float:
    retry_after = headers.get("retry-after")
    if retry_after is not None and _is_delay(retry_after):
        return float(retry_after)
    return full_jitter(rng, attempt)


def _is_delay(value: str) -> bool:
    """A finite, non-negative number of seconds. HTTP-date forms are ignored."""
    try:
        number = float(value)
    except ValueError:
        return False
    return math.isfinite(number) and number >= 0


def _problem(status: int, title: str, detail: str) -> Response:
    return Response(status, {"content-type": "application/problem+json"}, problem(status, title, detail))


def _json_status(status: int) -> Response:
    return Response(status, {"content-type": "application/json"}, b"{}")
