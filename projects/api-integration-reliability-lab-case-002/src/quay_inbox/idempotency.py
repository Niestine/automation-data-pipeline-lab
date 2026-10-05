"""Outbound Idempotency-Key error model from the expired HTTPAPI draft.

The draft expired on 18 April 2026 and is not an RFC. Keys are quoted
RFC 8941 strings. The lookup is ``(client_id, method, path, key)``. The
payload fingerprint sits beside that key: the same fingerprint replays
the stored result, and a different fingerprint is 422. The expiry is
longer than the publisher retry table and the 30-day replay horizon.
"""

from __future__ import annotations

import hashlib
import threading

from .horizons import IDEMPOTENCY_TTL_SECONDS
from .httpmsg import HttpResponse, problem


def parse_idempotency_key(value: str | None) -> str | None:
    """Return the inner string, or None when the header is not a quoted token."""

    if value is None or len(value) < 3 or not value.startswith('"') or not value.endswith('"'):
        return None
    inner = value[1:-1]
    for char in inner:
        code = ord(char)
        if code < 0x20 or code > 0x7E or char in '"\\':
            return None
    return inner


class IdempotencyResource:
    def __init__(self, clock, handler, *, ttl: int = IDEMPOTENCY_TTL_SECONDS) -> None:
        if ttl <= 0:
            raise ValueError("idempotency expiry must be positive")
        self.clock = clock
        self.handler = handler
        self.ttl = ttl
        self.rows: dict[tuple, dict] = {}
        self.lock = threading.Lock()
        self.handler_runs = 0

    def handle(
        self,
        *,
        client_id: str,
        method: str,
        path: str,
        header: str | None,
        body: bytes,
    ) -> HttpResponse:
        if method not in ("POST", "PATCH"):
            return problem(405, "Method not allowed", "Only POST and PATCH take an Idempotency-Key.")
        parsed = parse_idempotency_key(header)
        if parsed is None:
            return problem(
                400,
                "Idempotency-Key is missing",
                "This operation requires a quoted Idempotency-Key.",
            )
        fingerprint = hashlib.sha256(body).hexdigest()
        lookup = (client_id, method, path, parsed)
        now = self.clock.time()
        with self.lock:
            row = self.rows.get(lookup)
            if row is not None and now >= row["expires"]:
                self.rows.pop(lookup, None)
                row = None
            if row is None:
                self.rows[lookup] = {
                    "fingerprint": fingerprint,
                    "state": "in_progress",
                    "expires": now + self.ttl,
                    "stored": None,
                }
            elif row["state"] == "in_progress":
                return problem(
                    409,
                    "A request is outstanding for this Idempotency-Key",
                    "A request with the same Idempotency-Key for the same operation is being processed.",
                )
            elif row["fingerprint"] != fingerprint:
                return problem(
                    422,
                    "Idempotency-Key is already used",
                    "The same Idempotency-Key was presented with a different payload.",
                )
            else:
                status, stored, expires = row["stored"]
                return self._stored(status, stored, expires)
        try:
            self.handler_runs += 1
            status, stored = self.handler(method, path, body, client_id)
        except Exception:
            status, stored = 500, b'{"error":"handler"}'
        with self.lock:
            current = self.rows[lookup]
            current["state"] = "completed"
            current["stored"] = (status, stored, current["expires"])
            expires = current["expires"]
        return self._stored(status, stored, expires)

    def _stored(self, status: int, body: bytes, expires: int) -> HttpResponse:
        return HttpResponse(
            status,
            body,
            {
                "content-type": "application/json",
                "idempotency-expires": str(expires),
            },
        )
