"""Audience-scoped archive pages and idempotent restock orders."""

from __future__ import annotations

import json
import threading

from lotcycle.archive import Collection, add_to_head, build_collection
from lotcycle.clock import VirtualClock
from lotcycle.errors import SchemaError
from lotcycle.httputil import Request, Response
from lotcycle.params import (
    IDEMPOTENCY_TTL,
    ISSUER,
    MAC_KEY,
    PAGE_SIZE,
    STOCKED_PREFIXES,
)
from lotcycle.schema import (
    canonical_json,
    fingerprint,
    parse_idempotency_key,
    problem,
    scope_allows,
    validate_entry,
    validate_order,
)
from lotcycle.store import Store
from lotcycle.tokens import read_access


class ResourceServer:
    def __init__(self, store: Store, clock: VirtualClock, audience: str) -> None:
        self.store = store
        self.clock = clock
        self.audience = audience
        self.collections: dict[str, Collection] = {}
        self.page_size = PAGE_SIZE
        self.fail_order_commits = 0
        self.park_gate: threading.Event | None = None
        self._pages = threading.Lock()

    def load_entries(self, client_id: str, entries: list[dict]) -> Collection:
        checked = [validate_entry(entry) for entry in entries]
        collection = build_collection(checked, self.page_size)
        with self._pages:
            self.collections[client_id] = collection
        return collection

    def add_entry(self, client_id: str, entry: dict) -> None:
        checked = validate_entry(entry)
        with self._pages:
            add_to_head(self.collections[client_id], checked)

    def hide_archive(self, client_id: str, cursor: str, how: str) -> None:
        with self._pages:
            collection = self.collections[client_id]
            if how == "gone":
                collection.gone.add(cursor)
            elif how == "forbidden":
                collection.forbidden.add(cursor)
            elif how == "missing":
                collection.archives.pop(cursor, None)
            else:
                raise ValueError(how)

    def entry_ids(self, client_id: str) -> set[str]:
        with self._pages:
            return set(self.collections[client_id].entry_ids())

    def page_entry_ids(self, client_id: str, cursor: str | None) -> list[str]:
        with self._pages:
            collection = self.collections[client_id]
            if cursor is None:
                page = collection.head
            else:
                page = collection.archives[cursor]
            return [entry["id"] for entry in page.entries]

    def find_entry(self, client_id: str, entry_id: str) -> dict | None:
        with self._pages:
            collection = self.collections.get(client_id)
            if collection is None:
                return None
            pages = [collection.head, *collection.archives.values()]
            for page in pages:
                for entry in page.entries:
                    if entry["id"] == entry_id:
                        return dict(entry)
        return None

    def handle(self, request: Request) -> Response:
        parts = [part for part in request.path.split("/") if part]
        if not parts or parts[0] != self.audience:
            return _problem(404, "Not Found", "audience")
        route = parts[1:]
        required = "orders.write" if route == ["orders"] else "excursions.read"
        claims, denied = self._authorize(request, required)
        if denied is not None:
            return denied
        assert claims is not None
        if request.method == "GET" and route == ["entries"]:
            return self._document(claims["client_id"], None)
        if request.method == "GET" and len(route) == 2 and route[0] == "archives":
            return self._document(claims["client_id"], route[1])
        if request.method == "POST" and route == ["orders"]:
            return self._order(request, claims)
        if (
            request.method == "POST"
            and len(route) == 3
            and route[0] == "archives"
            and route[2] == "entries"
        ):
            return self._sealed_write(claims["client_id"], route[1])
        return _problem(404, "Not Found", "route")

    def _authorize(self, request: Request, required_scope: str):
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            return None, _problem(401, "Unauthorized", "invalid_token")
        try:
            claims = read_access(token, MAC_KEY)
        except ValueError:
            return None, _problem(401, "Unauthorized", "invalid_token")
        if claims.get("iss") != ISSUER or claims.get("aud") != self.audience:
            return None, _problem(401, "Unauthorized", "invalid_token")
        try:
            expires_at = float(claims["exp"])
        except (TypeError, ValueError):
            return None, _problem(401, "Unauthorized", "invalid_token")
        if expires_at <= self.clock.now():
            return None, _problem(401, "Unauthorized", "invalid_token")
        scope = claims.get("scope")
        if not isinstance(scope, str) or not scope_allows(scope, required_scope):
            return None, _problem(403, "Forbidden", "insufficient_scope")
        if not isinstance(claims.get("client_id"), str):
            return None, _problem(401, "Unauthorized", "invalid_token")
        return claims, None

    def _document(self, client_id: str, cursor: str | None) -> Response:
        with self._pages:
            collection = self.collections.get(client_id)
            if collection is None:
                return _problem(404, "Not Found", "collection")
            if cursor is not None and cursor in collection.forbidden:
                return _problem(403, "Forbidden", "archive refused")
            if cursor is not None and cursor in collection.gone:
                return _problem(410, "Gone", "archive gone")
            if cursor is None:
                page = collection.head
                page_id = "head"
            else:
                page = collection.archives.get(cursor)
                if page is None:
                    return _problem(404, "Not Found", "archive")
                page_id = "archive:" + cursor
            body = canonical_json(
                {
                    "entries": page.entries,
                    "id": page_id,
                    "updated": page.updated,
                }
            )
            headers = {"content-type": "application/json", "link": _links(self.audience, page)}
        return Response(200, headers, body)

    def _sealed_write(self, client_id: str, cursor: str) -> Response:
        # Every addressable archive page is sealed; only the head takes inserts.
        with self._pages:
            collection = self.collections.get(client_id)
            page = None if collection is None else collection.archives.get(cursor)
            if page is None:
                return _problem(404, "Not Found", "archive")
        return _problem(409, "Conflict", "sealed archive")

    def _order(self, request: Request, claims: dict) -> Response:
        if len(request.body) > 20 * 1024:
            return _problem(413, "Payload Too Large", "order body")
        try:
            key = parse_idempotency_key(request.headers.get("idempotency-key"))
        except SchemaError:
            return _problem(400, "Bad Request", "Idempotency-Key is required")
        try:
            document = json.loads(request.body.decode("utf-8"))
            order = validate_order(document)
        except (SchemaError, json.JSONDecodeError, UnicodeError):
            return _problem(400, "Bad Request", "order schema")
        digest = fingerprint(request.method, request.path, request.body)
        outcome = self._claim(claims["client_id"], request.path, key, digest)
        if outcome is not None:
            return outcome
        if self.park_gate is not None:
            self.park_gate.wait(3)
        return self._finish_order(claims["client_id"], request.path, key, order)

    def _claim(self, client_id: str, path: str, key: str, digest: str) -> Response | None:
        now = self.clock.now()
        with self.store.transaction():
            row = self._fresh_row(client_id, path, key, now)
            if row is None:
                self.store.con.execute(
                    """
                    INSERT INTO idempotency (
                        client_id, method, path, idem_key, fingerprint, state,
                        response_status, response_type, response_body, created_at
                    ) VALUES (?, 'POST', ?, ?, ?, 'in_flight', NULL, NULL, NULL, ?)
                    """,
                    (client_id, path, key, digest, now),
                )
                return None
            if row["fingerprint"] != digest:
                return _problem(422, "Unprocessable Content", "idempotency fingerprint")
            if row["state"] == "completed":
                return Response(
                    int(row["response_status"]),
                    {"content-type": row["response_type"]},
                    bytes(row["response_body"]),
                )
            return _problem(409, "Conflict", "idempotency key is in flight")

    def _fresh_row(self, client_id: str, path: str, key: str, now: float):
        row = self.store.idempotency_row(client_id, "POST", path, key)
        if row is None:
            return None
        if now - float(row["created_at"]) >= IDEMPOTENCY_TTL:
            self.store.con.execute(
                """
                DELETE FROM idempotency
                WHERE client_id = ? AND method = 'POST' AND path = ? AND idem_key = ?
                """,
                (client_id, path, key),
            )
            return None
        return row

    def _finish_order(self, client_id: str, path: str, key: str, order: dict) -> Response:
        now = self.clock.now()
        try:
            with self.store.transaction():
                if not order["sku"].startswith(STOCKED_PREFIXES):
                    body = problem(404, "Not Found", "sku is not stocked")
                    status = 404
                    content_type = "application/problem+json"
                else:
                    order_id = self.store.next_order_id()
                    self.store.con.execute(
                        """
                        INSERT INTO orders (order_id, client_id, sku, qty, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (order_id, client_id, order["sku"], order["qty"], now),
                    )
                    body = canonical_json(
                        {
                            "client_id": client_id,
                            "order_id": order_id,
                            "qty": order["qty"],
                            "sku": order["sku"],
                        }
                    )
                    status = 201
                    content_type = "application/json"
                    if self.fail_order_commits > 0:
                        # Fault injection: the business insert has run and the
                        # transaction aborts before the idempotency row completes.
                        self.fail_order_commits -= 1
                        raise _ForcedRollback(order_id)
                self.store.con.execute(
                    """
                    UPDATE idempotency
                    SET state = 'completed', response_status = ?, response_type = ?,
                        response_body = ?
                    WHERE client_id = ? AND method = 'POST' AND path = ? AND idem_key = ?
                    """,
                    (status, content_type, body, client_id, path, key),
                )
        except _ForcedRollback:
            # The order insert rolled back with it. Nothing completed, so the
            # in_flight row is removed and the same key may execute again.
            self._delete_inflight_autonomous(client_id, path, key)
            self.store.log("order_rollback", client_id=client_id)
            return _problem(500, "Server Error", "order rolled back")
        except Exception:
            self._delete_inflight_autonomous(client_id, path, key)
            raise
        self.store.log("order_completed", client_id=client_id, status=status)
        return Response(status, {"content-type": content_type}, body)

    def _delete_inflight(self, client_id: str, path: str, key: str) -> None:
        self.store.con.execute(
            """
            DELETE FROM idempotency
            WHERE client_id = ? AND method = 'POST' AND path = ? AND idem_key = ?
              AND state = 'in_flight'
            """,
            (client_id, path, key),
        )

    def _delete_inflight_autonomous(self, client_id: str, path: str, key: str) -> None:
        with self.store.transaction():
            self._delete_inflight(client_id, path, key)


class _ForcedRollback(Exception):
    """Injected failure after the order insert and before commit."""


def _problem(status: int, title: str, detail: str) -> Response:
    return Response(
        status,
        {"content-type": "application/problem+json"},
        problem(status, title, detail),
    )


def _links(audience: str, page) -> str:
    current = f"/{audience}/entries"
    if page.cursor is None:
        self_url = current
    else:
        self_url = f"/{audience}/archives/{page.cursor}"
    parts = [
        f'<{self_url}>; rel="self"',
        f'<{current}>; rel="current"',
    ]
    if page.prev_cursor:
        parts.append(
            f'</{audience}/archives/{page.prev_cursor}>; rel="prev-archive"'
        )
    if page.next_cursor:
        parts.append(
            f'</{audience}/archives/{page.next_cursor}>; rel="next-archive"'
        )
    return ", ".join(parts)
