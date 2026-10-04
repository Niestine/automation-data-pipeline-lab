"""Deterministic synthetic catalog, webhook events, and fault scripts."""

from __future__ import annotations

from typing import Any

STATUSES = ("pending", "paid", "shipped", "cancelled", "refunded")
CURRENCIES = ("USD", "EUR", "JPY")


def build_catalog(count: int = 24) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        status = STATUSES[(i - 1) % 5]
        currency = CURRENCIES[(i - 1) % 3]
        qty = 1 + (i % 3)
        unit = 1000 + i * 25
        items: list[dict[str, Any]] = [
            {"sku": f"SKU-{100 + i}", "qty": qty, "unit_cents": unit},
        ]
        if i % 4 == 0:
            items.append({"sku": f"SKU-{200 + i}", "qty": 1, "unit_cents": 500})
        amount = sum(item["qty"] * item["unit_cents"] for item in items)
        orders.append(
            {
                "id": f"ORD-{1000 + i}",
                "status": status,
                "amount_cents": amount,
                "currency": currency,
                "updated_at": f"2026-01-01T{i - 1:02d}:00:00Z",
                "version": 1 + ((i - 1) % 3),
                "customer_ref": f"CUST-{100 + i}",
                "items": items,
            }
        )
    return orders


def _bump(order: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    updated = dict(order)
    updated["items"] = [dict(item) for item in order["items"]]
    updated.update(overrides)
    return updated


def build_webhook_events(catalog: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    catalog = catalog if catalog is not None else build_catalog()
    by_id = {row["id"]: row for row in catalog}
    first = by_id["ORD-1001"]
    paid = _bump(
        first,
        status="paid",
        version=int(first["version"]) + 1,
        updated_at="2026-01-02T12:00:00Z",
    )
    third = by_id["ORD-1003"]
    cancelled = _bump(
        third,
        status="cancelled",
        version=int(third["version"]) + 1,
        updated_at="2026-01-02T13:00:00Z",
    )
    created = {
        "id": "ORD-1025",
        "status": "pending",
        "amount_cents": 2500,
        "currency": "USD",
        "updated_at": "2026-01-02T14:00:00Z",
        "version": 1,
        "customer_ref": "CUST-225",
        "items": [{"sku": "SKU-325", "qty": 1, "unit_cents": 2500}],
    }
    stale = _bump(by_id["ORD-1002"], version=1, updated_at="2026-01-01T00:30:00Z")
    def wrap(event_id: str, event_type: str, created_at: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": event_id,
            "type": event_type,
            "created_at": created_at,
            "data": _bump(data),
        }

    return [
        wrap("evt_0001", "order.updated", "2026-01-02T12:00:00Z", paid),
        wrap("evt_0001", "order.updated", "2026-01-02T12:00:00Z", paid),
        wrap("evt_0002", "order.updated", "2026-01-02T12:00:01Z", paid),
        wrap("evt_0003", "order.updated", "2026-01-02T12:05:00Z", stale),
        wrap("evt_0004", "order.created", "2026-01-02T14:00:00Z", created),
        wrap("evt_0005", "order.cancelled", "2026-01-02T13:00:00Z", cancelled),
    ]


def build_fault_script() -> list[dict[str, Any]]:
    return [
        {"method": "GET", "path": "/v1/orders", "status": 503},
        {
            "method": "GET",
            "path": "/v1/orders",
            "status": 429,
            "headers": {"Retry-After": "0"},
        },
        {"method": "GET", "path": "/v1/orders", "timeout": True},
        {"method": "POST", "path": "/v1/acks", "status": 500},
        {"method": "POST", "path": "/v1/acks", "lose_response": True},
    ]
