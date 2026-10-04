"""REST client: paginated reads, schema checks, idempotent writes."""

from __future__ import annotations

from typing import Any, Optional

from .errors import SchemaError
from .models import LAB_TOKEN, Order, PageResult
from .retry import RetryPolicy
from .schema import dumps_canonical, parse_json, validate_order_dict, validate_page_envelope
from .telemetry import JsonLogger, WallClock
from .transport import HttpRequest, InProcessTransport, RetryingTransport, raise_for_status


class OrdersClient:
    def __init__(
        self,
        transport: RetryingTransport,
        *,
        token: str = LAB_TOKEN,
        logger: Optional[JsonLogger] = None,
        default_limit: int = 10,
    ) -> None:
        self.transport = transport
        self.token = token
        self.logger = logger if logger is not None else JsonLogger()
        self.default_limit = default_limit

    def health(self) -> dict[str, Any]:
        response = self.transport.send(self._request("GET", "/health", auth=False))
        raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise SchemaError("health payload is invalid", errors=["$.status"])
        return payload

    def get_order(self, order_id: str) -> Order:
        response = self.transport.send(self._request("GET", f"/v1/orders/{order_id}"))
        raise_for_status(response)
        payload = response.json()
        errors = validate_order_dict(payload)
        if errors:
            raise SchemaError("order failed schema validation", errors=errors)
        return Order.from_validated(payload)

    def list_page(self, cursor: Optional[str] = None, limit: Optional[int] = None) -> PageResult:
        query = {"limit": str(limit if limit is not None else self.default_limit)}
        if cursor:
            query["cursor"] = cursor
        response = self.transport.send(self._request("GET", "/v1/orders", query=query))
        raise_for_status(response)
        payload = parse_json(response.body)
        envelope_errors = validate_page_envelope(payload)
        if envelope_errors:
            raise SchemaError("page envelope failed schema validation", errors=envelope_errors)

        valid: list[Order] = []
        rejected: list[dict[str, Any]] = []
        for index, raw in enumerate(payload["items"]):
            errors = validate_order_dict(raw)
            if errors:
                rejected.append({"index": index, "value": raw, "errors": errors})
                self.logger.log(
                    "order_rejected",
                    index=index,
                    errors=errors,
                    order_id=raw.get("id") if isinstance(raw, dict) else None,
                )
            else:
                valid.append(Order.from_validated(raw))

        short_page = bool(payload["has_more"] and len(payload["items"]) < payload["limit"])
        if short_page:
            self.logger.log(
                "short_page",
                item_count=len(payload["items"]),
                limit=payload["limit"],
                cursor=cursor,
            )
        self.logger.log(
            "page_fetched",
            item_count=len(valid),
            rejected=len(rejected),
            has_more=payload["has_more"],
            cursor=cursor,
            next_cursor=payload["next_cursor"],
            request_id=response.request_id,
        )
        return PageResult(
            items=tuple(valid),
            rejected=tuple(rejected),
            next_cursor=payload["next_cursor"],
            has_more=payload["has_more"],
            limit=payload["limit"],
            short_page=short_page,
            request_id=response.request_id,
        )

    def ack_shipment(self, order_id: str, version: int, *, idempotency_key: str) -> dict[str, Any]:
        body = {"order_id": order_id, "version": version, "action": "ack_shipment"}
        request = self._request(
            "POST",
            "/v1/acks",
            json_body=body,
            extra_headers={"idempotency-key": idempotency_key},
        )
        response = self.transport.send(request)
        raise_for_status(response)
        payload = response.json()
        replayed = response.headers.get("x-idempotent-replay") == "true"
        self.logger.log(
            "ack_sent",
            order_id=order_id,
            version=version,
            replayed=replayed,
            request_id=response.request_id,
        )
        if not isinstance(payload, dict):
            raise SchemaError("ack payload is invalid")
        payload = dict(payload)
        payload["replayed"] = replayed
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[dict[str, str]] = None,
        json_body: Any = None,
        extra_headers: Optional[dict[str, str]] = None,
        auth: bool = True,
    ) -> HttpRequest:
        headers ={"accept": "application/json", "user-agent": "api-reliability-lab/1.0"}
        if auth:
            headers["authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)
        body = b""
        if json_body is not None:
            body = dumps_canonical(json_body)
            headers["content-type"] = "application/json"
        return HttpRequest(method=method, path=path, query=query or {}, headers=headers, body=body)


def build_client(
    handler: Any,
    *,
    token: str = LAB_TOKEN,
    policy: Optional[RetryPolicy] = None,
    logger: Optional[JsonLogger] = None,
    clock: Any = None,
    sleeper: Any = None,
    seed: int = 7,
    default_limit: int = 10,
) -> tuple[OrdersClient, RetryingTransport]:
    logger = logger if logger is not None else JsonLogger()
    clock = clock if clock is not None else WallClock()
    inner = InProcessTransport(handler)
    retrying = RetryingTransport(
        inner,
        policy=policy,
        logger=logger,
        clock=clock,
        sleeper=sleeper if sleeper is not None else clock.sleep,
        seed=seed,
    )
    client = OrdersClient(retrying, token=token, logger=logger, default_limit=default_limit)
    return client, retrying
