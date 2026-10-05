"""Mock registrar API: snapshot cursors, idempotent upserts, signed webhooks."""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

from .errors import (
    InvalidArgument,
    MalformedIdempotencyKey,
    MissingIdempotencyKey,
    ResponseDropped,
    SchemaError,
    TokenDenied,
)
from .httpmsg import Request, Response
from .journal import Journal
from .schema import (
    LAB_WEBHOOK_SECRET,
    TOKEN_TTL_SECONDS,
    canonical_bytes,
    coerce_page_size,
    fingerprint,
    loads_strict,
    parse_caller,
    parse_filter,
    parse_idempotency_key,
    parse_order,
    require_parent,
    resource_body,
)
from .store import Store
from .webhooks import SignatureError, verify

_ROUTE = re.compile(r"^/v1/([a-z0-9][a-z0-9-]{0,63})/resources(?::upsert)?$")
_QUERY_KEYS = frozenset({"page_size", "page_token", "filter", "order"})
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


@dataclass
class Page:
    resources: list[dict]
    next_token: str
    snapshot_id: str
    request_token: str
    parent: str


def problem(status: int, slug: str, title: str, detail: str) -> Response:
    body = canonical_bytes(
        {
            "detail": detail,
            "status": status,
            "title": title,
            "type": f"https://lab.example/problems/{slug}",
        }
    )
    return Response(status, {"content-type": "application/problem+json"}, body)


def json_response(status: int, payload: dict, headers: dict[str, str] | None = None) -> Response:
    merged = {"content-type": "application/json"}
    if headers:
        merged.update(headers)
    return Response(status, merged, canonical_bytes(payload))


class Service:
    def __init__(
        self,
        store: Store,
        clock,
        journal: Journal,
        rng: random.Random,
        secrets: list[bytes] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.journal = journal
        self.rng = rng
        self.secrets = secrets if secrets is not None else [LAB_WEBHOOK_SECRET]
        self.drop_response_once = False
        self.pause_once = None
        self.fail_list: list[tuple[int, str | None]] = []
        self.empty_page_once = False
        self.withhold_terminal = 0
        self.fail_webhook_commit = False
        self.fail_upsert_commit = False
        self.last_limit: int | None = None
        self.page_trace: list[dict] = []

    def handle(self, request: Request) -> Response:
        if request.method == "POST" and request.path == "/webhooks/events":
            return self._webhook(request)
        match = _ROUTE.match(request.path)
        if not match:
            return problem(404, "not-found", "Not found", "No such route")
        parent = match.group(1)
        if request.path.endswith(":upsert"):
            if request.method != "POST":
                return problem(405, "method", "Method not allowed", "POST is required")
            return self._upsert(request, parent)
        if request.method != "GET":
            return problem(405, "method", "Method not allowed", "GET is required")
        return self._list(request, parent)

    def _caller(self, request: Request) -> str | None:
        return parse_caller(request.header("authorization"))

    def _list(self, request: Request, parent: str) -> Response:
        caller = self._caller(request)
        if caller is None:
            return problem(401, "unauthorized", "Unauthorized", "Bearer caller is required")
        if self.fail_list:
            status, retry_after = self.fail_list.pop(0)
            headers = {"content-type": "application/problem+json"}
            if retry_after is not None:
                headers["Retry-After"] = retry_after
            self.journal.record(
                "list_unavailable",
                "retry_same_request" if status in {429, 500, 502, 503, 504} else "surface_error",
                status=status,
            )
            body = canonical_bytes(
                {
                    "detail": "injected list failure",
                    "status": status,
                    "title": "List unavailable",
                    "type": "https://lab.example/problems/list-unavailable",
                }
            )
            return Response(status, headers, body)
        unknown = set(request.query) - _QUERY_KEYS
        if unknown:
            return problem(400, "invalid-argument", "Invalid argument", "unsupported query")
        try:
            page_size = coerce_page_size(request.query.get("page_size"))
            filter_raw, note_equals = parse_filter(request.query.get("filter"))
            order = parse_order(request.query.get("order"))
        except SchemaError as exc:
            return problem(400, "invalid-argument", "Invalid argument", str(exc))
        self.last_limit = page_size
        token = request.query.get("page_token") or ""
        try:
            page = self._list_page(caller, parent, page_size, filter_raw, note_equals, order, token)
        except InvalidArgument as exc:
            return problem(400, "invalid-argument", "Invalid argument", str(exc))
        except TokenDenied as exc:
            return problem(403, "forbidden", "Forbidden", str(exc))
        self.page_trace.append({"count": len(page.resources), "end": page.next_token == ""})
        return json_response(
            200,
            {
                "next_page_token": page.next_token,
                "resources": page.resources,
                "snapshot_id": page.snapshot_id,
            },
        )

    def _list_page(
        self,
        caller: str,
        parent: str,
        page_size: int,
        filter_raw: str,
        note_equals: str | None,
        order: str,
        token: str,
    ) -> Page:
        self.store.lock.acquire()
        try:
            self.store.begin()
            try:
                page = self._list_in_txn(
                    caller, parent, page_size, filter_raw, note_equals, order, token
                )
                self.store.commit()
                return page
            except Exception:
                self.store.rollback()
                raise
        finally:
            self.store.lock.release()

    def _list_in_txn(
        self,
        caller: str,
        parent: str,
        page_size: int,
        filter_raw: str,
        note_equals: str | None,
        order: str,
        token: str,
    ) -> Page:
        now = self.clock.now()
        if token:
            record = self.store.get_token(token)
            if record is None:
                raise InvalidArgument("unknown page token")
            if now >= float(record["expires_at"]):
                raise InvalidArgument("page token expired")
            if record["caller_id"] != caller:
                raise TokenDenied("page token belongs to another caller")
            if (
                record["parent"] != parent
                or record["filter_raw"] != filter_raw
                or record["order_by"] != order
            ):
                raise InvalidArgument("page token does not match the query")
            snapshot_id = record["snapshot_id"]
            cursor = None
            if record["has_cursor"]:
                cursor = (int(record["cursor_sort"]), record["cursor_id"])
            bound = None
            if record["bound_sort"] is not None:
                bound = (int(record["bound_sort"]), record["bound_id"])
            request_token = token
        else:
            snapshot_id = self._new_id("snap-")
            bound_sort, bound_id = self.store.copy_snapshot(
                snapshot_id,
                caller,
                parent,
                filter_raw,
                note_equals,
                order,
                now,
            )
            cursor = None
            bound = None if bound_sort is None else (bound_sort, bound_id)
            request_token = ""
        if self.empty_page_once:
            self.empty_page_once = False
            resources: list[dict] = []
            more = True
            next_cursor = cursor
            has_cursor = cursor is not None
        else:
            fetched = self.store.snapshot_page(
                snapshot_id, parent, cursor, bound, page_size + 1
            )
            more = len(fetched) > page_size
            resources = fetched[:page_size]
            if resources:
                last = resources[-1]
                next_cursor = (last["sort_key"], last["resource_id"])
                has_cursor = True
            else:
                next_cursor = cursor
                has_cursor = cursor is not None
            if not more and self.withhold_terminal > 0:
                self.withhold_terminal -= 1
                more = True
        if not more:
            next_token = ""
        else:
            next_token = self._new_id("")
            self.store.put_token(
                {
                    "token": next_token,
                    "caller_id": caller,
                    "parent": parent,
                    "filter_raw": filter_raw,
                    "order_by": order,
                    "snapshot_id": snapshot_id,
                    "cursor_sort": None if next_cursor is None else next_cursor[0],
                    "cursor_id": None if next_cursor is None else next_cursor[1],
                    "has_cursor": has_cursor,
                    "bound_sort": None if bound is None else bound[0],
                    "bound_id": None if bound is None else bound[1],
                    "expires_at": now + TOKEN_TTL_SECONDS,
                }
            )
        return Page(resources, next_token, snapshot_id, request_token, parent)

    def read_snapshot_page(
        self,
        caller: str,
        parent: str,
        snapshot_id: str,
        page_size: int,
        filter_raw: str,
        order: str,
    ) -> Page:
        """Re-read the first page of an existing snapshot. Does not open another."""
        self.store.lock.acquire()
        try:
            self.store.begin()
            try:
                meta = self.store.snapshot_meta(snapshot_id)
                if meta is None:
                    raise InvalidArgument("unknown snapshot")
                if meta["caller_id"] != caller:
                    raise TokenDenied("snapshot belongs to another caller")
                if meta["parent"] != parent or meta["filter_raw"] != filter_raw or meta["order_by"] != order:
                    raise InvalidArgument("snapshot does not match the query")
                bound = None
                if meta["bound_sort"] is not None:
                    bound = (int(meta["bound_sort"]), meta["bound_id"])
                fetched = self.store.snapshot_page(snapshot_id, parent, None, bound, page_size + 1)
                more = len(fetched) > page_size
                resources = fetched[:page_size]
                if not more:
                    next_token = ""
                else:
                    last = resources[-1]
                    next_token = self._new_id("")
                    self.store.put_token(
                        {
                            "token": next_token,
                            "caller_id": caller,
                            "parent": parent,
                            "filter_raw": filter_raw,
                            "order_by": order,
                            "snapshot_id": snapshot_id,
                            "cursor_sort": last["sort_key"],
                            "cursor_id": last["resource_id"],
                            "has_cursor": True,
                            "bound_sort": bound[0],
                            "bound_id": bound[1],
                            "expires_at": self.clock.now() + TOKEN_TTL_SECONDS,
                        }
                    )
                page = Page(resources, next_token, snapshot_id, "", parent)
                self.store.commit()
                return page
            except Exception:
                self.store.rollback()
                raise
        finally:
            self.store.lock.release()

    def _new_id(self, prefix: str) -> str:
        for _ in range(8):
            body = "".join(self.rng.choice(_ALPHABET) for _ in range(32))
            candidate = prefix + body
            if prefix == "" and self.store.get_token(candidate) is None:
                return candidate
            if prefix != "" and self.store.snapshot_meta(candidate) is None:
                return candidate
        raise RuntimeError("id space exhausted")

    def _upsert(self, request: Request, parent: str) -> Response:
        caller = self._caller(request)
        if caller is None:
            return problem(401, "unauthorized", "Unauthorized", "Bearer caller is required")
        try:
            key = parse_idempotency_key(request.header("idempotency-key"))
        except MissingIdempotencyKey as exc:
            return problem(400, "idempotency-missing", "Idempotency-Key is missing", str(exc))
        except MalformedIdempotencyKey as exc:
            return problem(400, "idempotency-malformed", "Idempotency-Key is malformed", str(exc))
        try:
            payload = loads_strict(request.body)
            fields = resource_body(payload, allow_request_id=True)
        except SchemaError as exc:
            return problem(400, "invalid-argument", "Invalid argument", str(exc))
        if_match = request.header("if-match")
        if if_match is not None and if_match.startswith("W/"):
            return problem(400, "invalid-argument", "Invalid argument", "weak validators are not compared")
        fp = fingerprint(parent, fields)
        now = self.clock.now()
        self.store.lock.acquire()
        claim = None
        try:
            self.store.begin()
            try:
                prepared = self.store.prepare_idempotency(caller, key, fp, now)
                if prepared["kind"] != "claim":
                    early = self._early_response(prepared)
                    self.store.commit()
                    return early
                claim = prepared["claim_token"]
                self.store.commit()
            except Exception:
                self.store.rollback()
                raise
        finally:
            self.store.lock.release()

        if self.pause_once is not None:
            gate = self.pause_once
            self.pause_once = None
            if not gate.wait(5):
                raise TimeoutError("upsert pause timed out")

        self.store.lock.acquire()
        try:
            self.store.begin()
            try:
                finished = self.store.finish_idempotency(
                    caller,
                    key,
                    claim,
                    parent,
                    fields,
                    if_match,
                    "",
                    self.clock.now(),
                )
                if self.fail_upsert_commit:
                    self.fail_upsert_commit = False
                    raise InjectedCommitFailure()
                self.store.commit()
            except Exception as exc:
                # A rolled-back handler must not leave the claim behind, or every
                # retry of this key would be frozen at 409 until the TTL.
                self.store.rollback()
                self.store.release_claim(caller, key, claim)
                if isinstance(exc, InjectedCommitFailure):
                    return problem(500, "commit-failed", "Commit failed", "upsert transaction rolled back")
                raise
            if finished["kind"] == "precondition":
                return problem(
                    412,
                    "precondition-failed",
                    "Precondition failed",
                    "If-Match does not match the current strong ETag",
                )
            if self.drop_response_once:
                self.drop_response_once = False
                self.journal.record("timeout_after_commit", "replay_stored_body", caller=caller)
                raise ResponseDropped("response dropped after commit")
            return json_response(
                200,
                json.loads(finished["body"]),
                {"etag": finished["etag"]},
            )
        finally:
            self.store.lock.release()

    def _early_response(self, prepared: dict) -> Response:
        kind = prepared["kind"]
        if kind == "mismatch":
            return problem(
                422,
                "idempotency-mismatch",
                "Idempotency-Key is already used",
                "The same key arrived with a different payload",
            )
        if kind == "in_progress":
            return problem(
                409,
                "idempotency-in-progress",
                "A request is outstanding for this Idempotency-Key",
                "Retry the same request without changing it",
            )
        if kind == "stored":
            headers = {"etag": prepared["etag"] or "", "x-idempotency-replay": "stored"}
            return Response(int(prepared["status"]), {**headers, "content-type": "application/json"}, prepared["body"].encode("utf-8"))
        if kind == "current":
            resource = self.store.current_resource_body(
                prepared["resource_parent"], prepared["resource_id"]
            )
            if resource is None:
                return problem(404, "not-found", "Not found", "The resource for this key is gone")
            return json_response(
                200,
                {"resource": resource},
                {"etag": resource["etag"], "x-idempotency-replay": "current-resource"},
            )
        raise RuntimeError("unknown idempotency result")

    def _webhook(self, request: Request) -> Response:
        raw = request.body
        if len(raw) > 20 * 1024:
            return problem(413, "payload-too-large", "Payload too large", "webhook body exceeds 20 KiB")
        webhook_id = request.header("webhook-id") or ""
        timestamp = request.header("webhook-timestamp") or ""
        signature = request.header("webhook-signature") or ""
        try:
            verify(
                webhook_id,
                timestamp,
                raw,
                signature,
                self.secrets,
                int(self.clock.now()),
            )
        except SignatureError as exc:
            self.journal.record("webhook_signature", "reject", reason=exc.reason)
            if exc.reason in {"dot", "id", "timestamp"}:
                return problem(400, "webhook-header", "Bad webhook header", exc.reason)
            return problem(401, "unauthorized", "Unauthorized", "webhook signature rejected")
        try:
            payload = loads_strict(raw)
            if not isinstance(payload, dict) or payload.get("type") != "allotment.revised":
                raise SchemaError("type must be allotment.revised")
            data = payload.get("data")
            if not isinstance(data, dict) or "parent" not in data:
                raise SchemaError("data.parent is required")
            parent = require_parent(data["parent"])
            fields = resource_body(
                {key: value for key, value in data.items() if key != "parent"},
                allow_request_id=False,
            )
        except SchemaError as exc:
            return problem(400, "invalid-argument", "Invalid argument", str(exc))
        self.store.lock.acquire()
        try:
            self.store.begin()
            try:
                if self.store.inbox_has(webhook_id):
                    self.store.commit()
                    self.journal.record("webhook_duplicate", "ack_without_write", webhook_id=webhook_id)
                    return Response(204, {}, b"")
                self.store.insert_inbox(webhook_id, self.clock.now())
                self.store.upsert_ledger_direct(parent, fields, "webhook")
                if self.fail_webhook_commit:
                    self.fail_webhook_commit = False
                    raise InjectedCommitFailure()
                self.store.commit()
            except InjectedCommitFailure:
                self.store.rollback()
                self.journal.record("webhook_commit", "return_5xx_no_inbox", webhook_id=webhook_id)
                return problem(500, "commit-failed", "Commit failed", "webhook transaction rolled back")
            except Exception:
                self.store.rollback()
                raise
        finally:
            self.store.lock.release()
        return Response(204, {}, b"")


class InjectedCommitFailure(Exception):
    """Internal signal so an injected upsert or webhook transaction rolls back."""
