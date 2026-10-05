"""Webhook receiver. 200 is written only after the inbox insert commits.

Authentication failures share one body. The body does not say which check
failed, and the log omits the payload, the secret, and the signature.

``/admin/replay`` is the one browser-reachable operator route, so it
requires ``x-csrf-token``. The webhook routes are exempt because the MAC
runs first. The admin route exists only when a replay callable is wired.
"""

from __future__ import annotations

import json

from .coverage import verify_coverage
from .errors import AuthError, SchemaError
from .horizons import BODY_CAP_BYTES
from .httpmsg import HttpRequest, HttpResponse, normalize_headers, plain
from .mac import verify_standard, verify_stripe
from .schema import loads, notice_from_standard, notice_from_stripe, parse_coverage_notice
from .secrets import Route
from .store import InboxRow, Store

UNAUTHORIZED = plain(401, "unauthorized")
ADMIN_PATH = "/admin/replay"


class Receiver:
    def __init__(
        self,
        store: Store,
        clock,
        routes: list[Route],
        *,
        dry_run: bool = False,
        ack_before_commit: bool = False,
        csrf_token: str = "csrf-lab-token",
        admin_replay=None,
    ) -> None:
        for route in routes:
            if route.tolerance <= 0:
                raise ValueError(
                    "tolerance must be a positive number of seconds; 0 disables the freshness check"
                )
        self.store = store
        self.clock = clock
        self.routes = {route.path: route for route in routes}
        self.dry_run = dry_run
        self.ack_before_commit = ack_before_commit
        self.csrf_token = csrf_token
        self.admin_replay = admin_replay
        self.auth_failures = 0
        self.accepted = 0
        self.duplicates = 0
        self.cache_suppressions = 0
        self.dry_runs = 0

    def handle(self, request: HttpRequest) -> HttpResponse:
        started = self.clock.time()
        event_id = None
        event_type = None
        route_id = ""
        status = 500
        try:
            response, event_id, event_type, route_id = self._dispatch(request)
            status = response.status
            return response
        finally:
            self.store.add_log(
                time=started,
                source=route_id,
                method=request.method,
                status=status,
                event_id=event_id,
                event_type=event_type,
                latency_ms=self.clock.time() - started,
            )

    def ingest(self, notice) -> str:
        """Catalog recovery uses the same claim as a verified push."""

        if self.dry_run:
            self.dry_runs += 1
            return "dry_run"
        return self._claim(notice, route_id="reconciler")

    def _dispatch(self, request: HttpRequest) -> tuple[HttpResponse, str | None, str | None, str]:
        if request.method != "POST":
            return plain(405, "method_not_allowed", allow="POST"), None, None, ""
        if len(request.body) > BODY_CAP_BYTES:
            return plain(413, "payload_too_large"), None, None, ""
        route = self.routes.get(request.path)
        if route is None:
            if request.path == ADMIN_PATH and self.admin_replay is not None:
                return self._admin(request), None, None, "admin"
            return plain(404, "not_found"), None, None, ""
        headers = normalize_headers(request.headers)
        try:
            verified_id, timestamp = self._verify(route, request, headers)
        except AuthError:
            self.auth_failures += 1
            claimed = headers.get("webhook-id") if route.profile == "standard" else None
            return UNAUTHORIZED, claimed, None, route.route_id
        try:
            payload = loads(request.body)
            notice = self._notice(route.profile, verified_id, payload)
        except SchemaError:
            return plain(400, "invalid_payload"), verified_id, None, route.route_id
        if self.ack_before_commit:
            return plain(200, "accepted"), notice.event_id, notice.event_type, route.route_id
        if self.dry_run:
            self.dry_runs += 1
            return plain(200, "dry_run"), notice.event_id, notice.event_type, route.route_id
        if self.store.fresh_seen(notice.event_id, self.clock.time()) and notice.event_id in self.store.inbox:
            self.cache_suppressions += 1
            self.duplicates += 1
            return plain(200, "accepted"), notice.event_id, notice.event_type, route.route_id
        outcome = self._claim(notice, route.route_id)
        if outcome == "inserted":
            self.store.fresh_remember(notice.event_id, timestamp, route.tolerance)
            self.accepted += 1
        else:
            self.duplicates += 1
        return plain(200, "accepted"), notice.event_id, notice.event_type, route.route_id

    def _admin(self, request: HttpRequest) -> HttpResponse:
        headers = normalize_headers(request.headers)
        if headers.get("x-csrf-token") != self.csrf_token:
            return plain(403, "forbidden")
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return plain(400, "invalid_payload")
        event_ids = payload.get("event_ids") if isinstance(payload, dict) else None
        if (
            not isinstance(event_ids, list)
            or not event_ids
            or not all(isinstance(item, str) and item for item in event_ids)
        ):
            return plain(400, "invalid_payload")
        try:
            self.admin_replay(event_ids)
        except KeyError:
            return plain(404, "unknown_event")
        body = json.dumps({"replayed": len(event_ids)}, separators=(",", ":")).encode("utf-8")
        return HttpResponse(202, body, {"content-type": "application/json"})

    def _claim(self, notice, route_id: str) -> str:
        row = InboxRow(
            event_id=notice.event_id,
            status="queued",
            notice=notice,
            accepted_at=self.clock.time(),
            route_id=route_id,
        )
        return self.store.claim(row)

    def _verify(self, route: Route, request: HttpRequest, headers: dict[str, str]) -> tuple[str | None, int]:
        now = self.clock.time()
        if route.profile == "standard":
            verified = verify_standard(
                request.body,
                headers,
                route.secrets,
                now=now,
                tolerance=route.tolerance,
            )
            return verified.event_id, verified.timestamp
        if route.profile == "stripe":
            verified = verify_stripe(
                request.body,
                headers.get("stripe-signature", ""),
                route.stripe_secrets,
                now=now,
                tolerance=route.tolerance,
            )
            return None, verified.timestamp
        verify_coverage(
            method=request.method,
            authority=request.authority,
            path=request.path,
            body=request.body,
            headers=headers,
            secrets=route.secrets,
            keyid=route.keyid,
        )
        return None, now

    def _notice(self, profile: str, verified_id: str | None, payload: dict):
        if profile == "standard":
            if not verified_id:
                raise SchemaError("event id")
            return notice_from_standard(verified_id, payload)
        if profile == "stripe":
            return notice_from_stripe(payload)
        return parse_coverage_notice(payload)
