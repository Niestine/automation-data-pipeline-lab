"""Two signature verifiers. They are not dialects of one function.

Standard Webhooks: HMAC-SHA256 over ``id.timestamp.raw_body``, key is the
decoded ``whsec_`` bytes, tags are space-separated ``v1,<base64>``.

Stripe fixture: HMAC-SHA256 over ``t.raw_body``, key is the endpoint secret
string, tags are comma-separated ``t=`` and ``v1=<hex>``. ``v0`` is ignored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass

from .errors import AuthError
from .horizons import TOLERANCE_SECONDS


@dataclass(frozen=True)
class Verified:
    event_id: str | None
    timestamp: int
    profile: str


def _require_tolerance(tolerance: int) -> None:
    if tolerance <= 0:
        raise ValueError(
            "tolerance must be a positive number of seconds; 0 disables the freshness check"
        )


def _within(timestamp: int, now: int, tolerance: int) -> bool:
    return abs(int(now) - int(timestamp)) <= int(tolerance)


def prefix_sha256(secret: bytes, body: bytes) -> bytes:
    """SHA-256(secret || body). A negative vector, not a verifier."""

    return hashlib.sha256(secret + body).digest()


def github_style_body_mac(secret: bytes, body: bytes) -> str:
    """Body-only HMAC. The delivery id is not inside the MAC.

    The receiver never calls this. It exists so tests can show a captured
    body staying valid under a substituted unsigned delivery id.
    """

    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def github_style_accepts(secret: bytes, body: bytes, header: str) -> bool:
    expected = github_style_body_mac(secret, body)
    if len(expected) != len(header):
        return False
    return hmac.compare_digest(expected, header)


def sign_standard(secret: bytes, event_id: str, timestamp: int, body: bytes) -> str:
    if "." in event_id or "." in str(timestamp):
        raise ValueError("id and timestamp must not contain '.'")
    signed = f"{event_id}.{int(timestamp)}.".encode("ascii") + body
    digest = hmac.new(secret, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def sign_standard_header(secrets: list[bytes], event_id: str, timestamp: int, body: bytes) -> str:
    return " ".join(sign_standard(secret, event_id, timestamp, body) for secret in secrets)


def verify_standard(
    body: bytes,
    headers: dict[str, str],
    secrets: list[bytes] | tuple[bytes, ...],
    *,
    now: int,
    tolerance: int = TOLERANCE_SECONDS,
) -> Verified:
    """Accept when any active secret matches a ``v1`` tag.

    A dot in the id or the timestamp is rejected before the MAC is computed.
    Tags other than ``v1`` are ignored, including ``v1a`` and ``v0``.
    """

    _require_tolerance(tolerance)
    event_id = headers.get("webhook-id", "")
    timestamp_text = headers.get("webhook-timestamp", "")
    signature = headers.get("webhook-signature", "")
    if not event_id or not timestamp_text or not signature:
        raise AuthError("missing")
    if "." in event_id or "." in timestamp_text:
        raise AuthError("dotted")
    if not timestamp_text.isdigit():
        raise AuthError("timestamp")
    timestamp = int(timestamp_text)
    if not _within(timestamp, now, tolerance):
        raise AuthError("stale")
    if not secrets:
        raise AuthError("route")
    signed = f"{event_id}.{timestamp_text}.".encode("ascii") + body
    for secret in secrets:
        expected = hmac.new(secret, signed, hashlib.sha256).digest()
        for token in signature.split(" "):
            if not token:
                continue
            version, separator, value = token.partition(",")
            if separator != "," or version != "v1" or not value:
                continue
            try:
                presented = base64.b64decode(value, validate=True)
            except Exception:
                continue
            if len(presented) != len(expected):
                continue
            if hmac.compare_digest(presented, expected):
                return Verified(event_id, timestamp, "standard")
    raise AuthError("mac")


def sign_stripe(
    secret: str,
    timestamp: int,
    body: bytes,
    *,
    include_v0: bool = False,
) -> str:
    signed = f"{int(timestamp)}.".encode("ascii") + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    header = f"t={int(timestamp)},v1={digest}"
    if include_v0:
        header += "," + "v0=" + ("0" * 64)
    return header


def sign_stripe_secrets(
    secrets: list[str],
    timestamp: int,
    body: bytes,
    *,
    include_v0: bool = False,
) -> str:
    parts = [sign_stripe(secret, timestamp, body, include_v0=False) for secret in secrets]
    # Each sign_stripe starts with t=. Keep a single timestamp element.
    v1s: list[str] = []
    timestamp_text = str(int(timestamp))
    for part in parts:
        for piece in part.split(","):
            if piece.startswith("v1="):
                v1s.append(piece)
    header = "t=" + timestamp_text + "," + ",".join(v1s)
    if include_v0:
        header += "," + "v0=" + ("0" * 64)
    return header


def verify_stripe(
    body: bytes,
    signature: str,
    secrets: list[str] | tuple[str, ...],
    *,
    now: int,
    tolerance: int = TOLERANCE_SECONDS,
) -> Verified:
    """Stripe manual procedure: secret string is the key, compare hex."""

    _require_tolerance(tolerance)
    if not signature or not secrets:
        raise AuthError("missing")
    timestamp_text: str | None = None
    v1_values: list[str] = []
    for piece in signature.split(","):
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        if name == "t" and timestamp_text is None:
            timestamp_text = value
        elif name == "v1" and value:
            v1_values.append(value)
        # v0 and every other scheme are discarded.
    if timestamp_text is None or not timestamp_text.isdigit() or not v1_values:
        raise AuthError("missing")
    timestamp = int(timestamp_text)
    if not _within(timestamp, now, tolerance):
        raise AuthError("stale")
    signed = f"{timestamp_text}.".encode("ascii") + body
    for secret in secrets:
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        for presented in v1_values:
            if len(presented) != len(expected):
                continue
            if hmac.compare_digest(presented, expected):
                return Verified(None, timestamp, "stripe")
    raise AuthError("mac")
