"""Destination ledger with versioned, idempotent upserts."""

from __future__ import annotations

from typing import Optional
import json
import os
from pathlib import Path

from .errors import CheckpointError
from .models import Order
from .schema import validate_order_dict


class Ledger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.orders: dict[str, Order] = {}
        self.sources: dict[str, str] = {}
        self.event_ids: set[str] = set()
        # Dry-run writes are staged here so later records in the same dry run
        # see them, but they are never flushed or visible through get().
        self._staged_orders: dict[str, Order] = {}
        self._staged_event_ids: set[str] = set()
        self.path = Path(path) if path is not None else None
        if self.path is not None and self.path.exists():
            self._load()

    def get(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    def seen_event(self, event_id: str) -> bool:
        return event_id in self.event_ids

    def discard_staged(self) -> None:
        self._staged_orders.clear()
        self._staged_event_ids.clear()

    def upsert(
        self,
        order: Order,
        *,
        source: str,
        event_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> str:
        if event_id and (
            event_id in self.event_ids or (dry_run and event_id in self._staged_event_ids)
        ):
            return "ignored_duplicate"

        existing = self.orders.get(order.id)
        if dry_run and order.id in self._staged_orders:
            existing = self._staged_orders[order.id]
        if existing is None:
            outcome = "inserted"
        elif order.version < existing.version:
            outcome = "ignored_stale"
        elif order.version == existing.version:
            if order.to_dict() == existing.to_dict():
                outcome = "ignored_duplicate"
            else:
                outcome = "ignored_conflict"
        else:
            outcome = "updated"

        if dry_run:
            if event_id:
                self._staged_event_ids.add(event_id)
            if outcome in {"inserted", "updated"}:
                self._staged_orders[order.id] = order
        else:
            if event_id:
                self.event_ids.add(event_id)
            if outcome in {"inserted", "updated"}:
                self.orders[order.id] = order
                self.sources[order.id] = source
            self._flush()
        return outcome

    def to_dict(self) -> dict:
        return {
            "orders": [order.to_dict() for order in self.orders.values()],
            "sources": dict(self.sources),
            "event_ids": sorted(self.event_ids),
        }

    def _flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise CheckpointError(f"could not write ledger {self.path}: {exc}") from exc

    def _load(self) -> None:
        assert self.path is not None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("ledger must be a JSON object")
            for row in data.get("orders") or []:
                errors = validate_order_dict(row)
                if errors:
                    raise ValueError(f"invalid order row: {errors[0]}")
                order = Order.from_validated(row)
                self.orders[order.id] = order
            self.sources = {str(k): str(v) for k, v in (data.get("sources") or {}).items()}
            self.event_ids = {str(item) for item in (data.get("event_ids") or [])}
        except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise CheckpointError(f"corrupt ledger {self.path}: {exc}") from exc
