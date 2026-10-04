"""Collect listing + product pages into a canonical snapshot and CSV."""

from __future__ import annotations

from typing import Any, Optional

from .catalog import CatalogStore
from .checkpoint import MemoryCheckpointStore
from .client import SiteClient
from .csv_export import render_csv, write_csv
from .decode import decode_html
from .detect import diff_products
from .errors import HttpError, ParseError, SchemaError, SimulatedCrash
from .models import (
    FIXTURE_ORIGIN,
    MAX_LISTING_PAGES,
    MAX_PRODUCTS,
    Checkpoint,
    CollectReport,
    Product,
    ms_to_iso_z,
)
from .normalize import normalize_product, same_origin_path
from .parse import parse_listing, parse_product
from .robots import RobotsPolicy, parse_robots
from .telemetry import JsonLogger, WallClock


class CollectJob:
    def __init__(
        self,
        client: SiteClient,
        *,
        catalog: Optional[CatalogStore] = None,
        previous: Optional[CatalogStore] = None,
        store: Any = None,
        logger: Optional[JsonLogger] = None,
        clock: Any = None,
        job_id: str = "lab-collect",
        origin: str = FIXTURE_ORIGIN,
        fail_fast: bool = False,
        crash_after_products: Optional[int] = None,
        csv_path: Any = None,
    ) -> None:
        self.client = client
        self.catalog = catalog if catalog is not None else CatalogStore()
        self.previous = previous if previous is not None else CatalogStore()
        self.store = store if store is not None else MemoryCheckpointStore()
        self.logger = logger if logger is not None else JsonLogger()
        self.clock = clock if clock is not None else WallClock()
        self.job_id = job_id
        self.origin = origin
        self.fail_fast = fail_fast
        self.crash_after_products = crash_after_products
        self.csv_path = csv_path
        self.robots: Optional[RobotsPolicy] = None

    def run(self, *, dry_run: bool = False, resume: bool = True) -> CollectReport:
        retries_before = self._retry_count()
        started_at = ms_to_iso_z(self.clock.now_ms())
        existing = self.store.load(self.job_id) if resume else None
        resumed = existing is not None and existing.status in {"in_progress", "failed"}
        if existing is not None and existing.status == "complete" and resume:
            # A finished weekly run is not skipped: the next invocation is a
            # new extract. Only an interrupted job is resumed.
            existing = None
            resumed = False
        if resumed:
            if self.catalog.path is not None and not self.catalog.products:
                self.catalog.load_file()
            # Rows already written by the interrupted run carry its start
            # stamp; keep one collected_at for the whole extract.
            if self.catalog.collected_at:
                started_at = self.catalog.collected_at
        else:
            # A fresh extract starts empty so rows from an older run (or a SKU
            # that has since left the site) cannot leak into this snapshot.
            self.catalog.products = {}
            self.catalog.etags = {}
        checkpoint = existing if existing is not None else Checkpoint(job_id=self.job_id)
        checkpoint.status = "in_progress"
        # Ordered list (not a set) so checkpoint files are deterministic.
        completed = list(checkpoint.completed_paths)
        report = CollectReport(
            job_id=self.job_id,
            dry_run=dry_run,
            resumed=resumed,
            collected_at=started_at,
        )
        self.catalog.collected_at = started_at
        self.catalog.job_id = self.job_id
        self.catalog.origin = self.origin
        if not resumed and not dry_run:
            self.catalog.persist()
        self._persist_checkpoint(checkpoint, dry_run=dry_run)
        self.logger.log(
            "collect_start",
            job_id=self.job_id,
            dry_run=dry_run,
            resumed=resumed,
            collected_at=started_at,
        )
        try:
            self._fetch_robots(checkpoint, report, dry_run=dry_run)
            hrefs = self._walk_listings(checkpoint, report, dry_run=dry_run)
            self._collect_products(
                hrefs,
                checkpoint,
                report,
                completed=completed,
                collected_at=started_at,
                dry_run=dry_run,
            )
            self._finish(checkpoint, report, dry_run=dry_run)
        except SimulatedCrash:
            checkpoint.status = "in_progress"
            self._persist_checkpoint(checkpoint, dry_run=dry_run)
            self.logger.log("collect_crash", job_id=self.job_id, last_path=checkpoint.last_path)
            raise
        except Exception:
            checkpoint.status = "failed"
            self._persist_checkpoint(checkpoint, dry_run=dry_run)
            raise
        report.retries = self._retry_count() - retries_before
        report.rate_limit_wait_ms = int(self.client.limiter.total_wait_ms)
        report.checkpoint = checkpoint.to_dict()
        return report

    def _fetch_robots(self, checkpoint: Checkpoint, report: CollectReport, *, dry_run: bool) -> None:
        if self.robots is not None:
            return
        try:
            response = self.client.get("/robots.txt")
        except HttpError as exc:
            if exc.status != 404:
                raise
            self.logger.log("robots_missing", status=404)
            self.robots = parse_robots("", self.client.user_agent)
            checkpoint.phase = "listing"
            checkpoint.updated_ms = self.clock.now_ms()
            self._persist_checkpoint(checkpoint, dry_run=dry_run)
            return
        text, _charset = decode_html(response.body, response.content_type)
        self.robots = parse_robots(text, self.client.user_agent)
        delay = self.robots.crawl_delay_ms
        if delay > 0:
            current = self.client.limiter.min_interval_ms
            self.client.limiter.set_min_interval_ms(max(current, delay))
        checkpoint.phase = "listing"
        checkpoint.updated_ms = self.clock.now_ms()
        self._persist_checkpoint(checkpoint, dry_run=dry_run)
        self.logger.log(
            "robots_fetched",
            crawl_delay_ms=delay,
            group_agents=list(self.robots.matched_group.agents) if self.robots.matched_group else [],
        )

    def _walk_listings(
        self,
        checkpoint: Checkpoint,
        report: CollectReport,
        *,
        dry_run: bool,
    ) -> list[str]:
        hrefs: list[str] = []
        seen_pages: set[str] = set()
        path = "/catalog"
        query: dict[str, str] = {}
        pages = 0
        policy = self.robots
        while pages < MAX_LISTING_PAGES:
            key = path if not query else f"{path}?page={query.get('page', '1')}"
            if key in seen_pages:
                break
            seen_pages.add(key)
            if policy is not None and not policy.allowed(path):
                report.robots_skipped += 1
                self.logger.log("robots_denied", path=key)
                break
            response = self.client.get(path, query=query)
            html, _charset = decode_html(response.body, response.content_type)
            listing = parse_listing(html)
            pages += 1
            report.listing_pages = pages
            checkpoint.listing_pages_done = pages
            checkpoint.last_path = key
            checkpoint.updated_ms = self.clock.now_ms()
            self._persist_checkpoint(checkpoint, dry_run=dry_run)
            self.logger.log("listing_page", path=key, cards=len(listing.cards), next=listing.next_href)
            for card in listing.cards:
                if not card.href:
                    continue
                resolved = same_origin_path(
                    card.href,
                    base=f"{self.origin}{path}",
                    origin=self.origin,
                )
                if resolved is None:
                    self.logger.log("off_origin_skipped", href=card.href)
                    continue
                product_path, _query = resolved
                hrefs.append(product_path)
            if not listing.next_href:
                break
            nxt = same_origin_path(
                listing.next_href,
                base=f"{self.origin}{path}",
                origin=self.origin,
            )
            if nxt is None:
                break
            path, query = nxt
        checkpoint.phase = "products"
        self._persist_checkpoint(checkpoint, dry_run=dry_run)
        return _unique(hrefs)

    def _collect_products(
        self,
        hrefs: list[str],
        checkpoint: Checkpoint,
        report: CollectReport,
        *,
        completed: list[str],
        collected_at: str,
        dry_run: bool,
    ) -> None:
        policy = self.robots
        for path in hrefs:
            if len(self.catalog.products) >= MAX_PRODUCTS:
                raise SchemaError("product cap exceeded")
            if policy is not None and not policy.allowed(path):
                report.robots_skipped += 1
                self.logger.log("robots_denied", path=path)
                continue
            if path in completed:
                continue
            etag = None if self.client.force else self.previous.etags.get(path)
            try:
                response = self.client.get(path, etag=etag)
            except HttpError as exc:
                if exc.status in {404, 410}:
                    report.products_rejected += 1
                    report.products_fetched += 1
                    completed.append(path)
                    checkpoint.completed_paths = list(completed)
                    checkpoint.rejected = report.products_rejected
                    checkpoint.last_path = path
                    checkpoint.updated_ms = self.clock.now_ms()
                    self._persist_checkpoint(checkpoint, dry_run=dry_run)
                    self.logger.log("product_missing", path=path, status=exc.status)
                    self._maybe_crash(report)
                    continue
                raise
            report.products_fetched += 1
            if response.status == 304:
                product = self._product_for_path(path)
                if product is None:
                    raise SchemaError(f"304 for {path} but no previous product")
                reused = Product(
                    sku=product.sku,
                    title=product.title,
                    price_cents=product.price_cents,
                    currency=product.currency,
                    availability=product.availability,
                    color=product.color,
                    size=product.size,
                    image_url=product.image_url,
                    source_url=product.source_url,
                    content_hash=product.content_hash,
                    collected_at=collected_at,
                    description=product.description,
                )
                self.catalog.upsert(reused)
                if response.etag:
                    self.catalog.remember_etag(path, response.etag)
                report.products_not_modified += 1
                report.products_parsed += 1
            else:
                try:
                    html, charset = decode_html(response.body, response.content_type)
                    parsed = parse_product(html)
                    source_url = f"{self.origin}{path}"
                    product = normalize_product(
                        parsed,
                        source_url=source_url,
                        collected_at=collected_at,
                        origin=self.origin,
                    )
                    self.catalog.upsert(product)
                    if response.etag:
                        self.catalog.remember_etag(path, response.etag)
                    report.products_parsed += 1
                    self.logger.log(
                        "product_parsed",
                        path=path,
                        sku=product.sku,
                        charset=charset,
                    )
                except (ParseError, SchemaError) as exc:
                    report.products_rejected += 1
                    self.logger.log(
                        "product_rejected",
                        path=path,
                        code=exc.code,
                        message=exc.message,
                        field=getattr(exc, "field", ""),
                    )
                    if self.fail_fast:
                        raise
            completed.append(path)
            checkpoint.completed_paths = list(completed)
            checkpoint.products_seen = report.products_parsed
            checkpoint.rejected = report.products_rejected
            checkpoint.last_path = path
            checkpoint.updated_ms = self.clock.now_ms()
            if not dry_run:
                self.catalog.persist()
            self._persist_checkpoint(checkpoint, dry_run=dry_run)
            self._maybe_crash(report)

    def _finish(self, checkpoint: Checkpoint, report: CollectReport, *, dry_run: bool) -> None:
        changes = diff_products(self.previous.sorted_products(), self.catalog.sorted_products())
        report.added = len(changes.added)
        report.removed = len(changes.removed)
        report.changed = len(changes.changed)
        report.unchanged = len(changes.unchanged)
        report.changes = changes.to_dict()
        report.csv_rows = len(self.catalog.products)
        checkpoint.phase = "complete"
        checkpoint.status = "complete"
        checkpoint.updated_ms = self.clock.now_ms()
        if not dry_run:
            self.catalog.persist()
            if self.csv_path is not None:
                write_csv(self.csv_path, self.catalog.sorted_products())
        else:
            render_csv(self.catalog.sorted_products())
        self._persist_checkpoint(checkpoint, dry_run=dry_run)
        self.logger.log(
            "collect_complete",
            job_id=self.job_id,
            added=report.added,
            removed=report.removed,
            changed=report.changed,
            unchanged=report.unchanged,
            rejected=report.products_rejected,
            dry_run=dry_run,
        )

    def _product_for_path(self, path: str) -> Optional[Product]:
        source = f"{self.origin}{path}"
        for item in self.previous.products.values():
            if item.source_url == source:
                return item
        return None

    def _maybe_crash(self, report: CollectReport) -> None:
        if self.crash_after_products is None:
            return
        if report.products_fetched >= self.crash_after_products:
            raise SimulatedCrash(
                f"crash after {report.products_fetched} product fetches"
            )

    def _persist_checkpoint(self, checkpoint: Checkpoint, *, dry_run: bool) -> None:
        if dry_run:
            return
        self.store.save(checkpoint)

    def _retry_count(self) -> int:
        transport = getattr(self.client, "transport", None)
        return int(getattr(transport, "retry_count", 0) or 0)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
