"""Fixed lab parameters. Durations are virtual seconds."""

from __future__ import annotations

ACCESS_LIFETIME = 60
INACTIVITY_WINDOW = 3 * ACCESS_LIFETIME
PRECOMMIT_BUDGET = 3
POST_SEND_BUDGET = 0
JITTER_BASE = 0.05
JITTER_CAP = 2.0
IDEMPOTENCY_TTL = 24 * 60 * 60
INFLIGHT_RETRIES = 3
RESOURCE_BUDGET = 3
WEBHOOK_TOLERANCE = 5 * 60
WEBHOOK_RETENTION = 5 * 60
RECEIVER_TIMEOUT = 15
DELIVERY_ATTEMPTS = 5
PAGE_SIZE = 50
PROOF_TOLERANCE = 5 * 60

ISSUER = "https://auth.coldlot.example"
# Shared by the in-process authorization server and resource server.
# Refresh tokens are the unguessable credential; this MAC key is a fixture.
MAC_KEY = b"lotcycle-lab-mac-v1"
SENDER_PEPPER = b"lotcycle-lab-sender-pepper"
WEBHOOK_PEPPER = b"lotcycle-lab-webhook-pepper"

TERMINAL_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)
RETRIABLE_STATUS = frozenset({500, 502, 503, 504})
ARCHIVE_STOP = frozenset({403, 404, 410})

TOKEN_HEADERS = {
    "cache-control": "no-store",
    "pragma": "no-cache",
    "content-type": "application/json;charset=UTF-8",
}

STOCKED_PREFIXES = ("crate-", "pack-")
