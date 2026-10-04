"""Weekly catalog ingest: decode, parse, adapt, validate, apply, checkpoint."""

from __future__ import annotations

from typing import Optional

from .catalog import CatalogStore, make_product
from .checkpoint import CheckpointStore, MemoryCheckpointStore, checkpoint_key
from .compat import adapt_row
from .decode import decode_feed
from .errors import SimulatedCrash, ValidationError
from .ledger import FeedLedger
from .models import (
    CanonicalRecord,
    Checkpoint,
    FeedInput,
    FeedReport,
    Product,
    RowOutcome,
    RunReport,
)
from .parse import parse_feed
from .persist import sha256_bytes
from .telemetry import JsonLogger, ManualClock
from .window import iso_week_id


class MaintenancePipeline:
    def __init__(
        self,
        catalog: CatalogStore,
        *,
        ledger: Optional[FeedLedger] = None,
        checkpoints: Optional[CheckpointStore] = None,
        logger: Optional[JsonLogger] = None,
        clock: Optional[ManualClock] = None,
        crash_after_rows: Optional[int] = None,
        fail_fast: bool = False,
        force: bool = False,
    ) -> None:
        self.catalog = catalog
        self.ledger = ledger if ledger is not None else FeedLedger()
        self.checkpoints = checkpoints if checkpoints is not None else MemoryCheckpointStore()
        self.logger = logger if logger is not None else JsonLogger()
        self.clock = clock if clock is not None else ManualClock()
        self.crash_after_rows = crash_after_rows
        self.fail_fast = fail_fast
        self.force = force
        self._applied_ok = 0

    def ingest(
        self,
        feeds: list[FeedInput],
        *,
        dry_run: bool = False,
        resume: bool = True,
    ) -> RunReport:
        week_id = iso_week_id(self.clock.now_ms())
        self.logger.log("run_start", week_id=week_id, dry_run=dry_run, feeds=len(feeds))
        if dry_run:
            self.catalog.begin_dry_run()
            self.ledger.begin_dry_run()
        report = RunReport(week_id=week_id, dry_run=dry_run, crash_after_rows=self.crash_after_rows)
        try:
            for feed in feeds:
                report.feeds.append(self._ingest_feed(feed, week_id=week_id, dry_run=dry_run, resume=resume))
        finally:
            if dry_run:
                self.catalog.abort_dry_run()
                self.ledger.abort_dry_run()
        report.catalog_size = len(self.catalog)
        self.logger.log("run_complete", week_id=week_id, catalog_size=report.catalog_size, **report.counts())
        return report

    def _ingest_feed(
        self,
        feed: FeedInput,
        *,
        week_id: str,
        dry_run: bool,
        resume: bool,
    ) -> FeedReport:
        digest = sha256_bytes(feed.data)
        self.logger.log("feed_start", name=feed.name, sha256=digest, week_id=week_id)
        prior = None if self.force else self.ledger.get(week_id, digest)
        if prior is not None and prior.get("status") == "complete":
            self.logger.log("feed_replay", name=feed.name, sha256=digest, bug_guards=["BUG-014"])
            return FeedReport(
                name=feed.name,
                encoding=str(prior.get("encoding") or "unknown"),
                format=str(prior.get("format") or "unknown"),
                version=str(prior.get("version") or "v1"),
                sha256=digest,
                week_id=week_id,
                replayed_feed=True,
                outcomes=(),
            )
        decoded = decode_feed(feed.data, declared_encoding=feed.declared_encoding)
        self.logger.log(
            "decode_ok",
            name=feed.name,
            encoding=decoded.encoding,
            bug_guards=list(decoded.bug_guards),
        )
        meta, rows = parse_feed(
            decoded.text,
            declared_version=feed.declared_version,
            declared_currency=feed.declared_currency,
        )
        skip_through = 0
        applied_skus: list[str] = []
        rejected = 0
        if resume and not dry_run:
            existing = self.checkpoints.load(checkpoint_key(week_id, feed.name))
            # Only an interrupted run resumes. A "complete" checkpoint is the
            # ledger's job; honouring it here would make --force skip every row.
            if (
                existing is not None
                and existing.status == "in_progress"
                and existing.feed_sha256 == digest
            ):
                skip_through = existing.last_row
                applied_skus = list(existing.applied_skus)
                rejected = existing.rejected
                self.logger.log(
                    "feed_resume",
                    name=feed.name,
                    last_row=skip_through,
                    interrupted_job=feed.name,
                )
        outcomes: list[RowOutcome] = []
        for row in rows:
            if row.index <= skip_through:
                continue
            try:
                canonical = adapt_row(row)
                if canonical.updated_at_ms > self.clock.now_ms():
                    raise ValidationError("updated_at is in the future", field="updated_at")
                outcome = self._apply_record(canonical, decode_guards=decoded.bug_guards)
            except ValidationError as exc:
                outcome = RowOutcome(
                    index=row.index,
                    sku=_peek_sku(row.fields.get("sku")),
                    status="rejected",
                    code=exc.code,
                    message=exc.message,
                    remaps=(),
                    bug_guards=exc.bug_guards,
                )
                rejected += 1
                self.logger.log(
                    "row_rejected",
                    name=feed.name,
                    index=row.index,
                    line=row.line_num,
                    sku=outcome.sku,
                    code=exc.code,
                    message=exc.message,
                    field=exc.field,
                    bug_guards=list(outcome.bug_guards),
                )
                if self.fail_fast:
                    self._save_checkpoint(
                        week_id,
                        feed.name,
                        digest,
                        row.index,
                        applied_skus,
                        rejected,
                        "in_progress",
                        dry_run,
                    )
                    raise
            outcomes.append(outcome)
            if outcome.status in {"inserted", "updated"} and outcome.sku:
                applied_skus.append(outcome.sku)
                self._applied_ok += 1
            self._save_checkpoint(
                week_id,
                feed.name,
                digest,
                row.index,
                applied_skus,
                rejected,
                "in_progress",
                dry_run,
            )
            if (
                self.crash_after_rows is not None
                and self._applied_ok >= self.crash_after_rows
                and outcome.status in {"inserted", "updated"}
            ):
                raise SimulatedCrash(
                    f"simulated crash after {self._applied_ok} applied rows in {feed.name}"
                )
        self._save_checkpoint(
            week_id,
            feed.name,
            digest,
            rows[-1].index if rows else 0,
            applied_skus,
            rejected,
            "complete",
            dry_run,
        )
        if not dry_run:
            self.ledger.record(
                week_id,
                digest,
                {
                    "status": "complete",
                    "name": feed.name,
                    "encoding": decoded.encoding,
                    "format": meta.get("format"),
                    "version": meta.get("version"),
                    "applied": len(applied_skus),
                    "rejected": rejected,
                },
            )
        report = FeedReport(
            name=feed.name,
            encoding=decoded.encoding,
            format=str(meta.get("format") or "csv"),
            version=str(meta.get("version") or "v1"),
            sha256=digest,
            week_id=week_id,
            replayed_feed=False,
            outcomes=tuple(outcomes),
        )
        self.logger.log("feed_complete", name=feed.name, **report.counts())
        return report

    def _apply_record(self, record: CanonicalRecord, *, decode_guards: tuple[str, ...]) -> RowOutcome:
        guards = tuple(dict.fromkeys(decode_guards + record.bug_guards))
        existing = self.catalog.get(record.sku)
        if existing is None:
            product = make_product(
                sku=record.sku,
                title=record.title,
                price_cents=record.price_cents,
                currency=record.currency,
                stock=0 if record.stock is None else record.stock,
                active=True if record.active is None else record.active,
                image_url=record.image_url,
                version=1,
                updated_at_ms=record.updated_at_ms,
                source_version=record.source_version,
            )
            self.catalog.put(product)
            status = "inserted"
            code = "inserted"
            message = "new sku"
            if record.stock is None:
                guards = tuple(dict.fromkeys(guards + ("BUG-005",)))
        else:
            status, code, message, product, extra_guards = _merge_existing(existing, record)
            guards = tuple(dict.fromkeys(guards + extra_guards))
            if status in {"inserted", "updated"}:
                self.catalog.put(product)
        self.logger.log(
            "row_applied",
            sku=record.sku,
            status=status,
            code=code,
            remaps=list(record.remaps),
            bug_guards=list(guards),
        )
        return RowOutcome(
            index=record.row_index,
            sku=record.sku,
            status=status,
            code=code,
            message=message,
            remaps=record.remaps,
            bug_guards=guards,
        )

    def _save_checkpoint(
        self,
        week_id: str,
        feed_name: str,
        digest: str,
        last_row: int,
        applied_skus: list[str],
        rejected: int,
        status: str,
        dry_run: bool,
    ) -> None:
        if dry_run:
            return
        self.checkpoints.save(
            Checkpoint(
                week_id=week_id,
                feed_name=feed_name,
                feed_sha256=digest,
                last_row=last_row,
                status=status,
                applied_skus=tuple(applied_skus),
                rejected=rejected,
            )
        )


def _merge_existing(
    existing: Product, record: CanonicalRecord
) -> tuple[str, str, str, Product, tuple[str, ...]]:
    guards: tuple[str, ...] = ()
    if record.updated_at_ms < existing.updated_at_ms:
        return "stale", "stale_snapshot", "incoming updated_at is older", existing, ("BUG-009",)
    merged_stock = existing.stock if record.stock is None else record.stock
    if record.stock is None:
        guards = ("BUG-005",)
    merged_active = existing.active if record.active is None else record.active
    merged_image = existing.image_url if record.image_url is None else record.image_url
    product = make_product(
        sku=existing.sku,
        title=record.title,
        price_cents=record.price_cents,
        currency=record.currency,
        stock=merged_stock,
        active=merged_active,
        image_url=merged_image,
        version=existing.version,
        updated_at_ms=record.updated_at_ms,
        source_version=record.source_version,
    )
    if record.updated_at_ms == existing.updated_at_ms:
        if product.fingerprint == existing.fingerprint:
            return "replayed", "idempotent", "same snapshot", existing, guards + ("BUG-014",)
        return "conflict", "idempotency_conflict", "same updated_at, different body", existing, ("BUG-009",)
    if product.fingerprint == existing.fingerprint:
        return "replayed", "noop", "no field changes", existing, guards + ("BUG-014",)
    bumped = make_product(
        sku=product.sku,
        title=product.title,
        price_cents=product.price_cents,
        currency=product.currency,
        stock=product.stock,
        active=product.active,
        image_url=product.image_url,
        version=existing.version + 1,
        updated_at_ms=product.updated_at_ms,
        source_version=product.source_version,
    )
    return "updated", "updated", "newer snapshot", bumped, guards


def _peek_sku(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    text = raw.strip().upper()
    return text or None


