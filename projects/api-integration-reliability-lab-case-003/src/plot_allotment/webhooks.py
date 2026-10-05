"""Standard Webhooks signatures and a shortened delivery ladder."""

from __future__ import annotations

import base64
import hashlib
import hmac
import random
import re
from dataclasses import dataclass

from .httpmsg import Response
from .journal import Journal
from .retry import NO_RETRY_STATUSES, full_jitter, parse_retry_after
from .schema import LAB_WEBHOOK_SECRET, canonical_bytes

# Lab stand-in for the specification table that ends near 75:35:05.
SHORT_LADDER_SECONDS = (0, 1, 5, 30, 120)
SPEC_LADDER_SECONDS = (0, 5, 300, 1800, 7200, 18000, 36000, 50400, 72000, 86400)
THROTTLE_STATUSES = frozenset({429, 502, 503, 504})


class SignatureError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def encode_whsec(secret: bytes) -> str:
    if not 24 <= len(secret) <= 64:
        raise ValueError("webhook secret must be 24 to 64 bytes")
    return "whsec_" + base64.b64encode(secret).decode("ascii")


def decode_whsec(token: str) -> bytes:
    if not token.startswith("whsec_"):
        raise ValueError("webhook secret must start with whsec_")
    raw = base64.b64decode(token[len("whsec_") :], validate=True)
    if not 24 <= len(raw) <= 64:
        raise ValueError("webhook secret must be 24 to 64 bytes")
    return raw


def sign(webhook_id: str, timestamp: str, raw_body: bytes, secret: bytes) -> str:
    message = f"{webhook_id}.{timestamp}.".encode("ascii") + raw_body
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def sign_rotation(
    webhook_id: str, timestamp: str, raw_body: bytes, secrets: list[bytes]
) -> str:
    return " ".join(sign(webhook_id, timestamp, raw_body, secret) for secret in secrets)


def verify(
    webhook_id: str,
    timestamp: str,
    raw_body: bytes,
    signature_header: str,
    secrets: list[bytes],
    now_s: int,
    skew_seconds: int = 300,
) -> None:
    if "." in webhook_id or "." in timestamp:
        raise SignatureError("dot")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", webhook_id):
        raise SignatureError("id")
    if not re.fullmatch(r"[0-9]+", timestamp):
        raise SignatureError("timestamp")
    if abs(int(now_s) - int(timestamp)) > skew_seconds:
        raise SignatureError("skew")
    matched = False
    for part in signature_header.split(" "):
        if not part.startswith("v1,"):
            continue
        supplied = part[3:]
        for secret in secrets:
            expected = sign(webhook_id, timestamp, raw_body, secret).split(",", 1)[1]
            if len(supplied) == len(expected) and hmac.compare_digest(supplied, expected):
                matched = True
                break
        if matched:
            break
    if not matched:
        raise SignatureError("mac")


@dataclass
class Endpoint:
    url: str
    disabled: bool = False


@dataclass
class Delivery:
    ok: bool
    attempts: int
    urls: list[str]
    sleeps: list[float]
    disabled: bool = False
    followed_redirect: bool = False
    location: str | None = None
    last_status: int | None = None


@dataclass
class WebhookSender:
    """Retries on the shortened ladder. 2xx is the only success."""

    endpoint: Endpoint
    clock: object
    rng: random.Random
    journal: Journal
    transport: object
    secret: bytes = LAB_WEBHOOK_SECRET
    webhook_id: str = "msg_allotment_0001"
    base: float = 0.05
    cap: float = 2.0

    def deliver(self, payload: dict) -> Delivery:
        if self.endpoint.disabled:
            self.journal.record("webhook_disabled", "stop_sending", webhook_id=self.webhook_id)
            return Delivery(ok=False, attempts=0, urls=[], sleeps=[], disabled=True)
        raw = canonical_bytes(payload)
        urls: list[str] = []
        sleeps: list[float] = []
        location = None
        followed = False
        last_status = None
        for index, _slot in enumerate(SHORT_LADDER_SECONDS):
            timestamp = str(int(self.clock.now()))
            headers = {
                "content-type": "application/json",
                "webhook-id": self.webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": sign(self.webhook_id, timestamp, raw, self.secret),
            }
            urls.append(self.endpoint.url)
            response: Response = self.transport(self.endpoint.url, raw, headers)
            last_status = response.status
            if 200 <= response.status < 300:
                return Delivery(
                    ok=True,
                    attempts=index + 1,
                    urls=urls,
                    sleeps=sleeps,
                    location=location,
                    followed_redirect=followed,
                    last_status=last_status,
                )
            if response.status == 410:
                self.endpoint.disabled = True
                self.journal.record("webhook_410", "disable_endpoint", webhook_id=self.webhook_id)
                return Delivery(
                    ok=False,
                    attempts=index + 1,
                    urls=urls,
                    sleeps=sleeps,
                    disabled=True,
                    location=location,
                    followed_redirect=False,
                    last_status=last_status,
                )
            if 300 <= response.status < 400:
                location = response.header("location")
                followed = False
                self.journal.record(
                    "webhook_redirect", "do_not_follow", webhook_id=self.webhook_id
                )
            if response.status in NO_RETRY_STATUSES:
                self.journal.record(
                    f"webhook_status_{response.status}",
                    "do_not_retry",
                    webhook_id=self.webhook_id,
                )
                return Delivery(
                    ok=False,
                    attempts=index + 1,
                    urls=urls,
                    sleeps=sleeps,
                    location=location,
                    followed_redirect=followed,
                    last_status=last_status,
                )
            if index + 1 == len(SHORT_LADDER_SECONDS):
                self.journal.record(
                    "webhook_budget", "surface_last_error", webhook_id=self.webhook_id
                )
                break
            retry_after = parse_retry_after(response.headers)
            if retry_after is not None:
                wait = retry_after
                decision = "wait_retry_after"
            elif response.status in THROTTLE_STATUSES:
                wait = full_jitter(index, self.base, self.cap, self.rng)
                decision = "full_jitter_retry"
            else:
                wait = float(SHORT_LADDER_SECONDS[index + 1])
                decision = "short_ladder"
            self.journal.record(
                f"webhook_status_{response.status}",
                decision,
                webhook_id=self.webhook_id,
                wait=wait,
            )
            sleeps.append(wait)
            self.clock.advance(wait)
        return Delivery(
            ok=False,
            attempts=len(urls),
            urls=urls,
            sleeps=sleeps,
            disabled=self.endpoint.disabled,
            location=location,
            followed_redirect=followed,
            last_status=last_status,
        )
