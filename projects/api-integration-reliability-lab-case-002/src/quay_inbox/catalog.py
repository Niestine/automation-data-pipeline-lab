"""Cursor list of release events, and the reconciler that shares the inbox.

Pages are newest-id first. ``starting_after`` walks toward older ids.
``ending_before`` walks toward newer ids. An insertion at the head does
not shift a cursor the way an offset page would.

The checkpoint's ``watermark_created`` is a point on the ``created`` axis:
the catalog time at which the last complete walk started, so every event
created at or before it was visible to that walk. It is not the dedupe key
and it is not the page cursor. If it falls more than 30 days behind, events
may have aged out of the list unseen, and the next run raises ``GapError``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ApiUsageError, GapError
from .horizons import LIST_RETENTION_SECONDS
from .schema import Notice


@dataclass
class Checkpoint:
    watermark_created: int | None = None
    last_id: str | None = None

    def to_json(self) -> dict:
        return {"version": 1, "watermark_created": self.watermark_created, "last_id": self.last_id}

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(self.to_json(), separators=(",", ":"), sort_keys=True).encode("utf-8")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, path)

    @classmethod
    def load(cls, path: Path) -> "Checkpoint":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError("unsupported checkpoint version")
        return cls(payload.get("watermark_created"), payload.get("last_id"))


class EventCatalog:
    def __init__(self, events: list[dict], clock) -> None:
        self.events = list(events)
        self.clock = clock

    def insert(self, event: dict) -> None:
        self.events.append(event)

    def list_events(
        self,
        *,
        starting_after: str | None = None,
        ending_before: str | None = None,
        limit: int = 10,
        delivery_success: bool | None = None,
        type: str | None = None,
        types: list[str] | None = None,
    ) -> dict:
        if type is not None and types is not None:
            raise ApiUsageError("type and types are mutually exclusive")
        if types is not None and len(types) > 20:
            raise ApiUsageError("types is capped at 20")
        if limit < 1:
            raise ApiUsageError("limit must be positive")
        now = self.clock.time()
        rows = []
        for event in self.events:
            if now - int(event["created"]) > LIST_RETENTION_SECONDS:
                continue
            if delivery_success is not None and bool(event["delivery_success"]) != delivery_success:
                continue
            if type is not None and event["type"] != type:
                continue
            if types is not None and event["type"] not in types:
                continue
            if starting_after is not None and event["id"] >= starting_after:
                continue
            if ending_before is not None and event["id"] <= ending_before:
                continue
            rows.append(event)
        rows.sort(key=lambda item: item["id"], reverse=True)
        page = rows[:limit]
        return {"data": page, "has_more": len(rows) > limit}


class Reconciler:
    def __init__(
        self,
        catalog: EventCatalog,
        receiver,
        checkpoint: Checkpoint,
        *,
        idempotency=None,
        limit: int = 3,
        delivery_success: bool | None = False,
    ) -> None:
        self.catalog = catalog
        self.receiver = receiver
        self.checkpoint = checkpoint
        self.idempotency = idempotency
        self.limit = limit
        self.delivery_success = delivery_success
        self.acked: list[str] = []

    def run(self, after_first_page=None) -> list[str]:
        started = self.catalog.clock.time()
        watermark = self.checkpoint.watermark_created
        if watermark is not None and started - watermark > LIST_RETENTION_SECONDS:
            raise GapError("checkpoint is older than 30 days")
        collected: list[str] = []
        cursor = None
        original_newest = None
        page_index = 0
        while True:
            page = self.catalog.list_events(
                starting_after=cursor,
                limit=self.limit,
                delivery_success=self.delivery_success,
            )
            data = page["data"]
            if not data:
                break
            page_index += 1
            if original_newest is None:
                original_newest = data[0]["id"]
            self._consume(data, collected)
            cursor = data[-1]["id"]
            if page_index == 1 and after_first_page is not None:
                after_first_page()
            if not page["has_more"]:
                break
        if original_newest is not None:
            self._catch_up(original_newest, collected)
        # Only a walk that reached the end, and claimed what it listed,
        # advances the checkpoint. A dry run leaves it where it was.
        if not getattr(self.receiver, "dry_run", False):
            self.checkpoint.watermark_created = started
            if collected:
                self.checkpoint.last_id = max(collected)
        return collected

    def _catch_up(self, original_newest: str, collected: list[str]) -> None:
        cursor = None
        while True:
            page = self.catalog.list_events(
                starting_after=cursor,
                ending_before=original_newest,
                limit=self.limit,
                delivery_success=self.delivery_success,
            )
            data = page["data"]
            if not data:
                break
            self._consume(data, collected)
            cursor = data[-1]["id"]
            if not page["has_more"]:
                break

    def _consume(self, data: list[dict], collected: list[str]) -> None:
        for event in data:
            collected.append(event["id"])
            notice = event["notice"]
            if not isinstance(notice, Notice):
                notice = Notice.from_json(notice)
            self.receiver.ingest(notice)
            if getattr(self.receiver, "dry_run", False):
                continue
            self._ack(event, notice)

    def _ack(self, event: dict, notice: Notice) -> None:
        if self.idempotency is None:
            return
        raw = json.dumps(
            {"event_id": event["id"], "object_id": notice.object_id},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        response = self.idempotency.handle(
            client_id="quay-reconciler",
            method="POST",
            path="/v1/releases/ack",
            header='"' + event["id"] + '"',
            body=raw,
        )
        if 200 <= response.status < 300:
            self.acked.append(event["id"])
