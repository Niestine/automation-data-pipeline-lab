"""In-process fulfillment API: cursor pages, faults, idempotent acks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import base64
import hashlib
import re

from .errors import TransportError
from .schema import dumps_canonical, parse_json, validate_ack_dict
from .transport import HttpRequest, HttpResponse


DETAIL_PATH = re.compile(r"^/v1/orders/(ORD-[0-9]{4,})$")


def encode_cursor(updated_at: str, order_id: str) -> str:
    raw = f"{updated_at}|{order_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    pad = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if "|" not in raw:
        raise ValueError("invalid cursor")
    updated_at, order_id = raw.split("|", 1)
    if not updated_at or not order_id:
        raise ValueError("invalid cursor")
    return updated_at, order_id


def _sort_key(order: dict[str, Any]) -> tuple[str, str]:
    return (str(order["updated_at"]), str(order["id"]))


@dataclass
class Fault:
    method: str
    path: str
    status: Optional[int] = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: bool = False
    malformed: bool = False
    truncate_envelope: bool = False
    raw_body: Optional[str] = None
    lose_response: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fault:
        if not isinstance(data, dict):
            raise ValueError("fault must be an object")
        if "method" not in data or "path" not in data:
            raise ValueError("fault requires method and path")
        headers = {str(k): str(v) for k, v in (data.get("headers") or {}).items()}
        status = data.get("status")
        if status is not None and (isinstance(status, bool) or not isinstance(status, int)):
            raise ValueError("fault.status must be an integer")
        return cls(
            method=str(data["method"]).upper(),
            path=str(data["path"]),
            status=status,
            headers=headers,
            timeout=bool(data.get("timeout")),
            malformed=bool(data.get("malformed")),
            truncate_envelope=bool(data.get("truncate_envelope")),
            raw_body=data.get("raw_body"),
            lose_response=bool(data.get("lose_response")),
        )


class MockFulfillmentApi:
    """Local source system. Catalog is mutable so tests can inject poison rows."""

    def __init__(
        self,
        orders: list[dict[str, Any]],
        *,
        token: str,
        faults: Optional[list[Fault | dict[str, Any]]] = None,
        default_limit: int = 10,
    ) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        for row in orders:
            copied = _copy_order(row)
            self.orders[copied["id"]] = copied
        self.token = token
        self.default_limit = default_limit
        self.faults: list[Fault] = [
            item if isinstance(item, Fault) else Fault.from_dict(item) for item in (faults or [])
        ]
        self.call_log: list[dict[str, Any]] = []
        self.ack_attempts: list[dict[str, Any]] = []
        self.ack_state: dict[str, int] = {}
        self._idempotency: dict[str, tuple[str, HttpResponse]] = {}
        self._req_n = 0

    def handle(self, request: HttpRequest) -> HttpResponse:
        self._req_n += 1
        request_id = f"req_{self._req_n:04d}"
        self.call_log.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "idempotency_key": request.headers.get("idempotency-key"),
                "request_id": request_id,
            }
        )
        fault = self._consume_fault(request)
        if fault is not None:
            if fault.timeout:
                raise TransportError("timeout", "mock timeout")
            if fault.raw_body is not None:
                return HttpResponse(
                    status=int(fault.status or 200),
                    headers={"content-type": "application/json", "x-request-id": request_id, **fault.headers},
                    body=fault.raw_body.encode("utf-8"),
                )
            if fault.malformed:
                return HttpResponse(
                    status=int(fault.status or 200),
                    headers={"content-type": "application/json", "x-request-id": request_id, **fault.headers},
                    body=b'{"object":"list"',
                )
            if fault.status is not None and fault.status >= 400:
                code = _code_for_status(fault.status)
                return self._json(
                    fault.status,
                    {"error": code, "code": code},
                    request_id=request_id,
                    extra_headers=fault.headers,
                )

        response = self._route(request, request_id, fault)
        if fault is not None and fault.lose_response:
            # The server committed the request but the client never sees the reply.
            raise TransportError("timeout", "mock timeout after server commit")
        return response

    def _route(self, request: HttpRequest, request_id: str, fault: Optional[Fault]) -> HttpResponse:
        if request.path == "/health" and request.method == "GET":
            return self._json(200, {"status": "ok"}, request_id=request_id)

        if not self._authorized(request):
            return self._json(401, {"error": "unauthorized", "code": "auth"}, request_id=request_id)

        if request.method == "GET" and request.path == "/v1/orders":
            response = self._list_orders(request, request_id)
            if fault is not None and fault.truncate_envelope and response.status == 200:
                payload = parse_json(response.body)
                payload.pop("next_cursor", None)
                return self._json(200, payload, request_id=request_id)
            return response
        detail = DETAIL_PATH.match(request.path)
        if request.method == "GET" and detail:
            return self._get_order(detail.group(1), request_id)
        if request.method == "POST" and request.path == "/v1/acks":
            return self._ack(request, request_id)
        return self._json(404, {"error": "not found", "code": "not_found"}, request_id=request_id)

    def upsert_order(self, order: dict[str, Any]) -> None:
        copied = _copy_order(order)
        self.orders[copied["id"]] = copied

    def _authorized(self, request: HttpRequest) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.token}"

    def _consume_fault(self, request: HttpRequest) -> Optional[Fault]:
        for index, fault in enumerate(self.faults):
            if fault.method == request.method and request.path == fault.path:
                return self.faults.pop(index)
        return None

    def _list_orders(self, request: HttpRequest, request_id: str) -> HttpResponse:
        raw_limit = request.query.get("limit", str(self.default_limit))
        try:
            limit = int(raw_limit)
        except ValueError:
            return self._json(400, {"error": "invalid limit", "code": "bad_request"}, request_id=request_id)
        if limit < 1 or limit > 50:
            return self._json(400, {"error": "invalid limit", "code": "bad_request"}, request_id=request_id)
        rows = sorted(self.orders.values(), key=_sort_key)
        start = 0
        cursor = request.query.get("cursor") or ""
        if cursor:
            try:
                last_ts, last_id = decode_cursor(cursor)
            except ValueError:
                return self._json(400, {"error": "invalid cursor", "code": "bad_request"}, request_id=request_id)
            start = len(rows)
            for index, row in enumerate(rows):
                if _sort_key(row) > (last_ts, last_id):
                    start = index
                    break
        chunk = [_copy_order(row) for row in rows[start : start + limit]]
        has_more = (start + limit) < len(rows)
        next_cursor = None
        if chunk and has_more:
            last = chunk[-1]
            next_cursor = encode_cursor(last["updated_at"], last["id"])
        payload = {
            "object": "list",
            "items": chunk,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "limit": limit,
        }
        return self._json(200, payload, request_id=request_id)

    def _get_order(self, order_id: str, request_id: str) -> HttpResponse:
        row = self.orders.get(order_id)
        if row is None:
            return self._json(404, {"error": "not found", "code": "not_found"}, request_id=request_id)
        return self._json(200, _copy_order(row), request_id=request_id)

    def _ack(self, request: HttpRequest, request_id: str) -> HttpResponse:
        key = request.headers.get("idempotency-key")
        if not key:
            return self._json(
                400,
                {"error": "Idempotency-Key is required", "code": "bad_request"},
                request_id=request_id,
            )
        try:
            payload = parse_json(request.body) if request.body else None
        except Exception:
            return self._json(400, {"error": "invalid JSON", "code": "bad_request"}, request_id=request_id)
        errors = validate_ack_dict(payload)
        if errors:
            return self._json(
                422,
                {"error": "unprocessable", "code": "unprocessable", "details": errors},
                request_id=request_id,
            )
        body_hash = hashlib.sha256(request.body).hexdigest()
        previous = self._idempotency.get(key)
        if previous is not None:
            stored_hash, stored = previous
            if stored_hash != body_hash:
                return self._json(
                    409,
                    {"error": "idempotency key reused with a different body", "code": "idempotency_conflict"},
                    request_id=request_id,
                )
            replay = HttpResponse(
                status=stored.status,
                headers={**stored.headers, "x-request-id": request_id, "x-idempotent-replay": "true"},
                body=stored.body,
            )
            self.ack_attempts.append({"order_id": payload["order_id"], "replay": True, "key": key})
            return replay

        order_id = payload["order_id"]
        version = payload["version"]
        order = self.orders.get(order_id)
        if order is None:
            return self._json(404, {"error": "not found", "code": "not_found"}, request_id=request_id)
        if order["status"] != "shipped":
            return self._json(
                422,
                {"error": "order is not shipped", "code": "unprocessable"},
                request_id=request_id,
            )
        if int(order["version"]) != int(version):
            return self._json(
                422,
                {"error": "version mismatch", "code": "unprocessable"},
                request_id=request_id,
            )

        already = order_id in self.ack_state
        self.ack_state[order_id] = int(version)
        self.ack_attempts.append({"order_id": order_id, "replay": False, "key": key})
        result = {
            "object": "ack",
            "order_id": order_id,
            "version": int(version),
            "duplicate": already,
        }
        response = self._json(200, result, request_id=request_id)
        self._idempotency[key] = (body_hash, response)
        return response

    def _json(
        self,
        status: int,
        payload: Any,
        *,
        request_id: str,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> HttpResponse:
        headers = {"content-type": "application/json", "x-request-id": request_id}
        if extra_headers:
            headers.update({k.lower(): v for k, v in extra_headers.items()})
        return HttpResponse(status=status, headers=headers, body=dumps_canonical(payload))


def _copy_order(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    items = row.get("items") or []
    copied["items"] = [dict(item) for item in items]
    return copied


def _code_for_status(status: int) -> str:
    return {
        400: "bad_request",
        401: "auth",
        403: "forbidden",
        404: "not_found",
        408: "timeout",
        409: "idempotency_conflict",
        422: "unprocessable",
        429: "rate_limited",
        500: "transient",
        502: "transient",
        503: "transient",
        504: "transient",
    }.get(status, "http_error")



