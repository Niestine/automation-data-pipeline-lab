"""Domain objects for the order-sync lab."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


ORDER_STATUSES = ("pending", "paid", "shipped", "cancelled", "refunded")
CURRENCIES = ("USD", "EUR", "JPY")
WEBHOOK_TYPES = ("order.created", "order.updated", "order.cancelled")
CHECKPOINT_PHASES = ("snapshot", "webhooks", "complete")
CHECKPOINT_STATUSES = ("in_progress", "complete", "failed")

LAB_TOKEN = "lab-bearer-token"
LAB_WEBHOOK_SECRET = "lab_webhook_secret_do_not_use_in_production"


@dataclass(frozen=True)
class OrderItem:
    sku: str
    qty: int
    unit_cents: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Order:
    id: str
    status: str
    amount_cents: int
    currency: str
    updated_at: str
    version: int
    customer_ref: str
    items: tuple[OrderItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "updated_at": self.updated_at,
            "version": self.version,
            "customer_ref": self.customer_ref,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_validated(cls, data: dict[str, Any]) -> Order:
        items = tuple(
            OrderItem(sku=item["sku"], qty=item["qty"], unit_cents=item["unit_cents"])
            for item in data["items"]
        )
        return cls(
            id=data["id"],
            status=data["status"],
            amount_cents=data["amount_cents"],
            currency=data["currency"],
            updated_at=data["updated_at"],
            version=data["version"],
            customer_ref=data["customer_ref"],
            items=items,
        )


@dataclass(frozen=True)
class PageResult:
    items: tuple[Order, ...]
    rejected: tuple[dict[str, Any], ...]
    next_cursor: Optional[str]
    has_more: bool
    limit: int
    short_page: bool
    request_id: Optional[str] = None


@dataclass
class Checkpoint:
    sync_id: str
    phase: str = "snapshot"
    status: str = "in_progress"
    cursor: Optional[str] = None
    pages_done: int = 0
    last_order_id: Optional[str] = None
    last_event_id: Optional[str] = None
    orders_seen: int = 0
    updated_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync_id": self.sync_id,
            "phase": self.phase,
            "status": self.status,
            "cursor": self.cursor,
            "pages_done": self.pages_done,
            "last_order_id": self.last_order_id,
            "last_event_id": self.last_event_id,
            "orders_seen": self.orders_seen,
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        if not isinstance(data, dict):
            raise ValueError("checkpoint must be an object")
        sync_id = str(data.get("sync_id") or "").strip()
        if not sync_id:
            raise ValueError("checkpoint.sync_id is required")
        phase = str(data.get("phase") or "snapshot")
        status = str(data.get("status") or "in_progress")
        if phase not in CHECKPOINT_PHASES:
            raise ValueError(f"checkpoint.phase {phase!r} is invalid")
        if status not in CHECKPOINT_STATUSES:
            raise ValueError(f"checkpoint.status {status!r} is invalid")
        return cls(
            sync_id=sync_id,
            phase=phase,
            status=status,
            cursor=data.get("cursor"),
            pages_done=int(data.get("pages_done") or 0),
            last_order_id=data.get("last_order_id"),
            last_event_id=data.get("last_event_id"),
            orders_seen=int(data.get("orders_seen") or 0),
            updated_ms=int(data.get("updated_ms") or 0),
        )


@dataclass
class SyncReport:
    sync_id: str
    dry_run: bool
    resumed: bool
    pages: int = 0
    retries: int = 0
    orders_inserted: int = 0
    orders_updated: int = 0
    orders_ignored_stale: int = 0
    orders_ignored_duplicate: int = 0
    orders_ignored_conflict: int = 0
    orders_rejected: int = 0
    acks_sent: int = 0
    acks_replayed: int = 0
    webhooks_applied: int = 0
    webhooks_duplicate: int = 0
    webhooks_stale: int = 0
    webhooks_conflict: int = 0
    webhooks_rejected: int = 0
    short_pages: int = 0
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def note_upsert(self, outcome: str) -> None:
        if outcome == "inserted":
            self.orders_inserted += 1
        elif outcome == "updated":
            self.orders_updated += 1
        elif outcome == "ignored_stale":
            self.orders_ignored_stale += 1
        elif outcome == "ignored_duplicate":
            self.orders_ignored_duplicate += 1
        elif outcome == "ignored_conflict":
            self.orders_ignored_conflict += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "sync_id": self.sync_id,
            "dry_run": self.dry_run,
            "resumed": self.resumed,
            "pages": self.pages,
            "retries": self.retries,
            "orders_inserted": self.orders_inserted,
            "orders_updated": self.orders_updated,
            "orders_ignored_stale": self.orders_ignored_stale,
            "orders_ignored_duplicate": self.orders_ignored_duplicate,
            "orders_ignored_conflict": self.orders_ignored_conflict,
            "orders_rejected": self.orders_rejected,
            "acks_sent": self.acks_sent,
            "acks_replayed": self.acks_replayed,
            "webhooks_applied": self.webhooks_applied,
            "webhooks_duplicate": self.webhooks_duplicate,
            "webhooks_stale": self.webhooks_stale,
            "webhooks_conflict": self.webhooks_conflict,
            "webhooks_rejected": self.webhooks_rejected,
            "short_pages": self.short_pages,
            "checkpoint": dict(self.checkpoint),
        }
