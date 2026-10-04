"""Snapshot + webhook drain with checkpoints, crash injection, and dry-run."""

from __future__ import annotations

from typing import Any, Optional

from .checkpoint import MemoryCheckpointStore
from .client import OrdersClient
from .errors import SchemaError, SimulatedCrash
from .ledger import Ledger
from .models import Checkpoint, Order, SyncReport
from .telemetry import JsonLogger, WallClock
from .webhooks import DeliveryResult, WebhookReceiver


class SyncJob:
    def __init__(
        self,
        client: OrdersClient,
        *,
        ledger: Optional[Ledger] = None,
        store: Any = None,
        receiver: Optional[WebhookReceiver] = None,
        logger: Optional[JsonLogger] = None,
        clock: Any = None,
        sync_id: str = "lab-sync",
        fail_fast: bool = False,
        crash_after_pages: Optional[int] = None,
        crash_at: str = "post_upsert",
        page_limit: int = 10,
    ) -> None:
        if crash_at not in {"post_upsert", "post_checkpoint", "post_ack"}:
            raise ValueError(f"unknown crash_at {crash_at!r}")
        self.client = client
        self.ledger = ledger if ledger is not None else Ledger()
        self.store = store if store is not None else MemoryCheckpointStore()
        self.logger = logger if logger is not None else JsonLogger()
        self.clock = clock if clock is not None else WallClock()
        self.receiver = receiver or WebhookReceiver(
            self.ledger, clock=self.clock, logger=self.logger
        )
        self.sync_id = sync_id
        self.fail_fast = fail_fast
        self.crash_after_pages = crash_after_pages
        self.crash_at = crash_at
        self.page_limit = page_limit

    def run(
        self,
        *,
        deliveries: Optional[list[tuple[dict[str, str], bytes]]] = None,
        dry_run: bool = False,
        resume: bool = True,
    ) -> SyncReport:
        retries_before = self._retry_count()
        self.ledger.discard_staged()
        existing =self.store.load(self.sync_id) if resume else None
        # A failed checkpoint is resumed from its cursor just like an in-progress one.
        resumed = existing is not None
        checkpoint = existing if existing is not None else Checkpoint(sync_id=self.sync_id)
        if checkpoint.status == "complete" and resume:
            report = SyncReport(
                sync_id=self.sync_id,
                dry_run=dry_run,
                resumed=True,
                checkpoint=checkpoint.to_dict(),
            )
            self.logger.log("sync_already_complete", sync_id=self.sync_id)
            return report

        checkpoint.status = "in_progress"
        report = SyncReport(sync_id=self.sync_id, dry_run=dry_run, resumed=resumed)
        self._persist(checkpoint, dry_run=dry_run)
        self.logger.log(
            "sync_start",
            sync_id=self.sync_id,
            dry_run=dry_run,
            resumed=resumed,
            phase=checkpoint.phase,
            cursor=checkpoint.cursor,
        )

        try:
            self.client.health()
            if checkpoint.phase == "snapshot":
                self._snapshot(checkpoint, report, dry_run=dry_run)
                checkpoint.phase = "webhooks"
                self._persist(checkpoint, dry_run=dry_run)
            if checkpoint.phase == "webhooks":
                self._webhooks(checkpoint, report, deliveries or [], dry_run=dry_run)
                checkpoint.phase = "complete"
                checkpoint.status = "complete"
                self._persist(checkpoint, dry_run=dry_run)
        except SimulatedCrash:
            checkpoint.status = "in_progress"
            self.logger.log(
                "sync_crash",
                sync_id=self.sync_id,
                pages_done=checkpoint.pages_done,
                cursor=checkpoint.cursor,
                crash_at=self.crash_at,
            )
            raise
        except Exception:
            checkpoint.status = "failed"
            if not dry_run:
                checkpoint.updated_ms = self.clock.now_ms()
                self.store.save(checkpoint)
            raise

        report.retries = self._retry_count() - retries_before
        report.checkpoint = checkpoint.to_dict()
        self.logger.log("sync_complete", report=report.to_dict())
        return report

    def _retry_count(self) -> int:
        return int(getattr(self.client.transport, "retry_count", 0))

    def _snapshot(self, checkpoint: Checkpoint, report: SyncReport, *, dry_run: bool) -> None:
        cursor = checkpoint.cursor
        pages_since_start = 0
        while True:
            page = self.client.list_page(cursor=cursor, limit=self.page_limit)
            pages_since_start += 1
            report.pages += 1
            if page.short_page:
                report.short_pages += 1
            for order in page.items:
                outcome = self.ledger.upsert(order, source="snapshot", dry_run=dry_run)
                report.note_upsert(outcome)
                self.logger.log(
                    "order_upserted",
                    order_id=order.id,
                    version=order.version,
                    outcome=outcome,
                    source="snapshot",
                    dry_run=dry_run,
                )

            if page.rejected:
                report.orders_rejected += len(page.rejected)
                if self.fail_fast:
                    raise SchemaError(
                        "fail-fast: poison item on page",
                        errors=[str(item.get("errors")) for item in page.rejected],
                    )

            self._maybe_crash(checkpoint, pages_since_start, "post_upsert")
            if not dry_run:
                self._ack_shipped(page.items, report)
            self._maybe_crash(checkpoint, pages_since_start, "post_ack")

            if page.items:
                checkpoint.last_order_id = page.items[-1].id
                checkpoint.orders_seen += len(page.items)
            checkpoint.cursor = page.next_cursor
            checkpoint.pages_done += 1
            checkpoint.updated_ms = self.clock.now_ms()
            self._persist(checkpoint, dry_run=dry_run)
            self._maybe_crash(checkpoint, pages_since_start, "post_checkpoint")

            if not page.has_more:
                break
            cursor = page.next_cursor

    def _ack_shipped(self, orders: tuple[Order, ...], report: SyncReport) -> None:
        for order in orders:
            if order.status != "shipped":
                continue
            key = f"{self.sync_id}:ack:{order.id}:{order.version}"
            result = self.client.ack_shipment(order.id, order.version, idempotency_key=key)
            if result.get("replayed"):
                report.acks_replayed += 1
            else:
                report.acks_sent += 1

    def _webhooks(
        self,
        checkpoint: Checkpoint,
        report: SyncReport,
        deliveries: list[tuple[dict[str, str], bytes]],
        *,
        dry_run: bool,
    ) -> None:
        for headers, body in deliveries:
            result = self.receiver.handle(headers, body, dry_run=dry_run)
            self._note_delivery(report, result)
            if result.status == 200 and result.event_id:
                if _event_after(result.event_id, checkpoint.last_event_id):
                    checkpoint.last_event_id = result.event_id
                    checkpoint.updated_ms = self.clock.now_ms()
                    self._persist(checkpoint, dry_run=dry_run)

    def _note_delivery(self, report: SyncReport, result: DeliveryResult) -> None:
        if result.status != 200:
            report.webhooks_rejected += 1
            return
        if result.outcome == "applied":
            report.webhooks_applied += 1
        elif result.outcome == "duplicate":
            report.webhooks_duplicate += 1
        elif result.outcome == "stale":
            report.webhooks_stale += 1
        elif result.outcome == "conflict":
            report.webhooks_conflict += 1
        else:
            report.webhooks_rejected += 1

    def _persist(self, checkpoint: Checkpoint, *, dry_run: bool) -> None:
        if dry_run:
            return
        checkpoint.updated_ms = self.clock.now_ms()
        self.store.save(checkpoint)
        self.logger.log(
            "checkpoint_saved",
            phase=checkpoint.phase,
            cursor=checkpoint.cursor,
            pages_done=checkpoint.pages_done,
            last_event_id=checkpoint.last_event_id,
        )

    def _maybe_crash(self, checkpoint: Checkpoint, pages_since_start: int, where: str) -> None:
        if self.crash_after_pages is None:
            return
        if where != self.crash_at:
            return
        if pages_since_start >= self.crash_after_pages:
            raise SimulatedCrash(
                f"simulated crash at {where} after {pages_since_start} page(s)"
            )


def _event_after(event_id: str, last_event_id: Optional[str]) -> bool:
    if last_event_id is None:
        return True
    return event_id > last_event_id
