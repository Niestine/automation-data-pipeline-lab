"""Opaque refresh tokens and HMAC access tokens.

Refresh tokens are 32 bytes from the operating system CSPRNG, encoded
without padding, and stored only as a SHA-256 hash. Timers use a seeded
RNG for reproducibility; token material never does. Access tokens are JSON plus an HMAC. They are not
JWTs. The resource server checks the MAC before it reads claims.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Callable

from lotcycle.params import MAC_KEY, SENDER_PEPPER


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_refresh_token(token_bytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    return b64url(token_bytes(32))


def issue_access(payload: dict, key: bytes = MAC_KEY) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    mac = hmac.new(key, raw, hashlib.sha256).digest()
    return b64url(raw) + "." + b64url(mac)


def read_access(token: str, key: bytes = MAC_KEY) -> dict:
    """Return the payload or raise ValueError when the MAC does not match."""
    try:
        raw_part, mac_part = token.split(".", 1)
        raw = b64url_decode(raw_part)
        mac = b64url_decode(mac_part)
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed access token") from exc
    expected = hmac.new(key, raw, hashlib.sha256).digest()
    if len(mac) != len(expected) or not hmac.compare_digest(mac, expected):
        raise ValueError("access token mac")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("access token payload")
    return payload


def unverified_payload(token: str) -> dict:
    """Client-side read of exp. The resource server never uses this."""
    raw_part = token.split(".", 1)[0]
    payload = json.loads(b64url_decode(raw_part).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("access token payload")
    return payload


def sender_key(key_id: str) -> bytes:
    return hmac.new(SENDER_PEPPER, key_id.encode("utf-8"), hashlib.sha256).digest()


def make_sender_proof(key: bytes, method: str, path: str, timestamp: str) -> str:
    message = f"{method}.{path}.{timestamp}".encode("utf-8")
    return base64.b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode("ascii")


def proof_matches(key: bytes, method: str, path: str, timestamp: str, presented: str) -> bool:
    expected = make_sender_proof(key, method, path, timestamp)
    if len(expected) != len(presented):
        return False
    return hmac.compare_digest(expected, presented)


def parse_basic(header: str) -> tuple[str, str]:
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "basic" or not rest:
        raise ValueError("not basic")
    try:
        decoded = base64.b64decode(rest.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("basic encoding") from exc
    client_id, sep, secret = decoded.partition(":")
    if not sep or not client_id:
        raise ValueError("basic form")
    return client_id, secret


def secret_matches(presented: str, expected: str) -> bool:
    if len(presented) != len(expected):
        return False
    return hmac.compare_digest(presented, expected)
