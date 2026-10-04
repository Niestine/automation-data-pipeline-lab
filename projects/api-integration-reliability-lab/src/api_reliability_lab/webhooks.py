"""HMAC webhook verification, replay window, and snapshot application."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import hmac
import hashlib
import re

from .errors import SchemaError
from .ledger import Ledger
from .models import LAB_WEBHOOK_SECRET, Order
from .schema import dumps_canonical, parse_json, validate_webhook_dict
from .telemetry import JsonLogger, WallClock
from .transport import normalize_headers


REPLAY_WINDOW_S = 300


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def build_delivery(
    event: dict[str, Any],
    *,
    secret: str,
    timestamp: str,
    body: Optional[bytes] = None,
) -> tuple[dict[str, str], bytes]:
    raw =body if body is not None else dumps_canonical(event)
    signature = sign_webhook(secret, timestamp, raw)
    headers = {
        "content-type": "application/json",
        "x-webhook-timestamp": timestamp,
        "x-webhook-signature": signature,
    }
    return headers, raw


@dataclass
class DeliveryResult:
    status: int
    outcome: str
    order_id: Optional[str] = None
    event_id: Optional[str] = None
    ledger_outcome: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "outcome": self.outcome,
            "order_id": self.order_id,
            "event_id": self.event_id,
            "ledger_outcome": self.ledger_outcome,
            "errors": list(self.errors),
        }


class WebhookReceiver:
    """HTTP-shaped receiver. Duplicates and stale events still return 200."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        secret: str = LAB_WEBHOOK_SECRET,
        clock: Any = None,
        logger: Optional[JsonLogger] = None,
        replay_window_s: int = REPLAY_WINDOW_S,
    ) -> None:
        self.ledger = ledger
        self.secret = secret
        self.clock = clock if clock is not None else WallClock()
        self.logger = logger if logger is not None else JsonLogger()
        self.replay_window_s = replay_window_s

    def handle(self, headers: dict[str, str], body: bytes, *, dry_run: bool = False) -> DeliveryResult:
        headers = normalize_headers(headers)
        timestamp = headers.get("x-webhook-timestamp") or ""
        signature = headers.get("x-webhook-signature") or ""
        if re.fullmatch(r"[0-9]{1,12}", timestamp) is None:
            return self._reject("replay", 401, "missing or invalid timestamp")
        now_s = self.clock.now_s()
        ts = int(timestamp)
        if abs(now_s - ts) > self.replay_window_s:
            self.logger.log("webhook_replay", timestamp=ts, now_s=now_s)
            return self._reject("replay", 401, "timestamp outside replay window")

        expected = sign_webhook(self.secret, timestamp, body)
        # Compare bytes: str compare_digest raises TypeError on non-ASCII input.
        if not hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8")):
            self.logger.log("webhook_bad_signature")
            return self._reject("bad_signature", 401, "signature mismatch")

        try:
            payload = parse_json(body)
        except SchemaError as exc:
            return self._reject("invalid_schema", 400, exc.message, errors=exc.errors)
        errors = validate_webhook_dict(payload)
        if errors:
            self.logger.log("webhook_rejected", errors=errors, event_id=None)
            return self._reject("invalid_schema", 400, "webhook failed schema validation", errors=errors)

        event_id = payload["id"]
        order = Order.from_validated(payload["data"])
        ledger_outcome = self.ledger.upsert(
            order, source="webhook", event_id=event_id, dry_run=dry_run
        )
        mapped = {
            "inserted": "applied",
            "updated": "applied",
            "ignored_duplicate": "duplicate",
            "ignored_stale": "stale",
            "ignored_conflict": "conflict",
        }[ledger_outcome]
        self.logger.log(
            "webhook_accepted",
            event_id=event_id,
            order_id=order.id,
            outcome=mapped,
            ledger_outcome=ledger_outcome,
            dry_run=dry_run,
        )
        return DeliveryResult(
            status=200,
            outcome=mapped,
            order_id=order.id,
            event_id=event_id,
            ledger_outcome=ledger_outcome,
        )

    def _reject(
        self,
        outcome: str,
        status: int,
        message: str,
        errors: Optional[list[str]] = None,
    ) -> DeliveryResult:
        detail = list(errors or [message])
        self.logger.log("webhook_rejected", outcome=outcome, status=status, errors=detail)
        return DeliveryResult(status=status, outcome=outcome, errors=detail)
