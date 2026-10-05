"""Acyclic export: list a snapshot page, upsert it, then advance the barrier."""

from __future__ import annotations

from dataclasses import dataclass

from .client import ExportClient
from .errors import CheckpointIOError, LeaseDenied
from .journal import Journal
from .schema import param_fingerprint
from .service import Service
from .store import Store

LEASE_SECONDS = 60.0


@dataclass
class ExportReport:
    snapshot_id: str | None
    pages: int
    seen_ids: list[str]
    replayed: bool
    dry_run: bool


class ExportWorker:
    def __init__(
        self,
        service: Service,
        store: Store,
        client: ExportClient,
        journal: Journal,
        clock,
        owner: str,
    ) -> None:
        self.service = service
        self.store = store
        self.client = client
        self.journal = journal
        self.clock = clock
        self.owner = owner
        self.hold_after_lease = None
        self.dry_run = False

    def run(
        self,
        scope: str,
        parent: str,
        filter_raw: str,
        order: str,
        page_size: int,
        mode: str = "atomic",
        max_pages: int | None = None,
    ) -> ExportReport:
        if mode not in {"atomic", "split"}:
            raise ValueError("mode must be atomic or split")
        if self.dry_run:
            return self._dry_run(parent, filter_raw, order, page_size)
        fp = param_fingerprint(parent, filter_raw, order)
        if not self.store.try_acquire(scope, self.owner, self.clock.now(), LEASE_SECONDS, fp):
            self.journal.record("lease_denied", "exit_without_writes", scope=scope)
            raise LeaseDenied(scope)
        if self.hold_after_lease is not None:
            entered, release = self.hold_after_lease
            self.hold_after_lease = None
            entered.set()
            if not release.wait(5):
                raise TimeoutError("lease hold timed out")
        replayed = False
        pages = 0
        seen: list[str] = []
        snapshot_id = None
        while True:
            checkpoint = self.store.checkpoint(scope)
            if checkpoint is None:
                raise LeaseDenied(scope)
            if checkpoint["done"]:
                break
            if not self.store.renew_lease(scope, self.owner, self.clock.now(), LEASE_SECONDS):
                self.journal.record("lease_lost", "stop_without_barrier", scope=scope)
                raise LeaseDenied(scope)
            if checkpoint["apply_ahead"]:
                self.journal.record("gap_detected", "replay_uncheckpointed_page", scope=scope)
                replayed = True
                page = self._fetch_gap(checkpoint, parent, filter_raw, order, page_size)
            elif checkpoint["next_token"] is None:
                page = self.client.list_page(parent, page_size, filter_raw, order, None)
            else:
                page = self.client.list_page(
                    parent, page_size, filter_raw, order, checkpoint["next_token"]
                )
            snapshot_id = page.snapshot_id
            seen.extend(row["resource_id"] for row in page.resources)
            self._persist(scope, page, mode)
            pages += 1
            if page.next_token == "":
                break
            if max_pages is not None and pages >= max_pages:
                break
        self.store.release_lease(scope, self.owner)
        return ExportReport(snapshot_id, pages, seen, replayed, False)

    def _dry_run(self, parent: str, filter_raw: str, order: str, page_size: int) -> ExportReport:
        token = None
        seen: list[str] = []
        pages = 0
        snapshot_id = None
        seen_tokens: set[str] = set()
        while True:
            page = self.client.list_page(parent, page_size, filter_raw, order, token)
            pages += 1
            snapshot_id = page.snapshot_id
            seen.extend(row["resource_id"] for row in page.resources)
            if not page.next_token:
                break
            if page.next_token in seen_tokens:
                raise RuntimeError("repeated page token")
            seen_tokens.add(page.next_token)
            token = page.next_token
        return ExportReport(snapshot_id, pages, seen, False, True)

    def _fetch_gap(self, checkpoint: dict, parent: str, filter_raw: str, order: str, page_size: int):
        applied = checkpoint["applied_token"] or ""
        if applied:
            return self.client.list_page(parent, page_size, filter_raw, order, applied)
        snapshot_id = checkpoint["snapshot_id"]
        if not snapshot_id:
            raise CheckpointIOError("gap has no snapshot")
        return self.service.read_snapshot_page(
            self.client.caller, parent, snapshot_id, page_size, filter_raw, order
        )

    def _persist(self, scope: str, page, mode: str) -> None:
        fetched = page.request_token
        try:
            if mode == "split":
                self.store.apply_split(
                    scope,
                    self.owner,
                    self.client.caller,
                    page.parent,
                    page.snapshot_id,
                    page.resources,
                    fetched,
                    self.clock.now(),
                )
                self.store.write_barrier(
                    scope, self.owner, fetched, page.next_token, page.snapshot_id
                )
            else:
                self.store.apply_atomic(
                    scope,
                    self.owner,
                    self.client.caller,
                    page.parent,
                    page.snapshot_id,
                    page.resources,
                    fetched,
                    page.next_token,
                    self.clock.now(),
                )
        except CheckpointIOError:
            self.journal.record("checkpoint_io", "surface_error", scope=scope)
            raise
        self.journal.record("barrier_advanced", "resume_at_following_token", scope=scope)
