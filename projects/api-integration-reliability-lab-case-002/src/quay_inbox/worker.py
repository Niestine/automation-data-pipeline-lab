"""Worker and downstream gate.

The business row and the memo are written in one locked update. An external
gate send happens after that commit. The gate dedupes on the event id, so a
crash between send and the completion mark still leaves one effect.
"""

from __future__ import annotations

from .errors import SimulatedCrash
from .schema import business_view
from .store import Store


class Upstream:
    def __init__(self, objects: dict[str, dict] | None = None) -> None:
        self.objects = objects or {}

    def fetch(self, object_id: str) -> dict:
        if object_id not in self.objects:
            raise KeyError(object_id)
        return dict(self.objects[object_id])


class Downstream:
    """Fake yard gate. The second send of an event id does not add a row."""

    def __init__(self) -> None:
        self.applied: dict[str, dict] = {}
        self.attempts = 0

    def accept(self, key: str, body: dict) -> str:
        self.attempts += 1
        if key not in self.applied:
            self.applied[key] = dict(body)
            return "applied"
        return "replay"


class Worker:
    def __init__(self, store: Store, upstream: Upstream, downstream: Downstream) -> None:
        self.store = store
        self.upstream = upstream
        self.downstream = downstream
        self.crash_after_send = False
        self.application_order: list[str] = []

    def drain(self) -> None:
        for event_id, row in list(self.store.inbox.items()):
            if row.status == "queued":
                self.apply(event_id)
            if row.status == "ready":
                self.dispatch_one(event_id)

    def apply(self, event_id: str) -> dict | None:
        with self.store.lock:
            row = self.store.inbox[event_id]
            key = (0, (event_id, "apply"))
            if key in self.store.memos:
                self.store.memo_hits += 1
                return self.store.memos[key]
            notice = row.notice
            applied = self._resolve(notice)
            if applied is None:
                return None
            previous = self.store.releases.get(notice.object_id)
            if previous != applied:
                self.store.release_changes += 1
            self.store.releases[notice.object_id] = applied
            pair = (notice.object_id, notice.event_type)
            self.store.pair_seen[pair] = event_id
            if notice.created is not None:
                last = self.store.last_created.get(notice.object_id)
                if last is None or notice.created >= last:
                    self.store.last_created[notice.object_id] = notice.created
            self.store.outbox[event_id] = {"status": "pending", "key": event_id, "body": applied}
            row.status = "ready"
            value = {"object_id": notice.object_id, "state": applied}
            self.store.memos[key] = value
            self.store.memo_misses += 1
            self.application_order.append(event_id)
            return value

    def dispatch_one(self, event_id: str) -> None:
        row = self.store.outbox.get(event_id)
        if row is None or row["status"] != "pending":
            return
        self.downstream.accept(row["key"], row["body"])
        if self.crash_after_send:
            self.crash_after_send = False
            raise SimulatedCrash(event_id)
        with self.store.lock:
            self.store.outbox[event_id]["status"] = "done"
            self.store.inbox[event_id].status = "done"

    def _resolve(self, notice) -> dict | None:
        pair = (notice.object_id, notice.event_type)
        last = self.store.last_created.get(notice.object_id)
        stale_time = notice.created is not None and last is not None and notice.created <= last
        if notice.thin or pair in self.store.pair_seen or stale_time:
            try:
                current = self.upstream.fetch(notice.object_id)
            except KeyError:
                return None
            self.store.fetches += 1
            return business_view(current, notice.api_version)
        return business_view(notice.snapshot, notice.api_version)
