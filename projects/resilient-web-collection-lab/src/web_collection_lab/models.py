"""Domain objects for the web collection lab."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


FIXTURE_ORIGIN = "https://fixture.example.invalid"
USER_AGENT = "LabCollector/1.0 (+https://collection.example.invalid/lab)"
# Frozen clock used by the CLI and tests: 2026-01-04T12:00:00Z.
LAB_NOW_MS = 1_767_528_000_000

CURRENCIES = ("USD", "EUR", "JPY")
CURRENCY_DECIMALS = {"USD": 2, "EUR": 2, "JPY": 0}
AVAILABILITIES = ("in_stock", "out_of_stock", "discontinued")
CHECKPOINT_PHASES = ("robots", "listing", "products", "complete")
CHECKPOINT_STATUSES = ("in_progress", "complete", "failed")

CSV_FIELDS = (
    "sku",
    "title",
    "price_cents",
    "currency",
    "availability",
    "color",
    "size",
    "image_url",
    "source_url",
    "content_hash",
    "collected_at",
)

SKU_PATTERN = r"^SKU-[A-Z0-9]{3,12}$"
TITLE_MAX = 200
COLOR_MAX = 40
SIZE_MAX = 16
DESCRIPTION_MAX = 500
MAX_LISTING_PAGES = 50
MAX_PRODUCTS = 500
MAX_CRAWL_DELAY_MS = 60_000


def ms_to_iso_z(ms: int) -> str:
    seconds = int(ms) // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_z_to_ms(value: str) -> int:
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True)
class Product:
    sku: str
    title: str
    price_cents: int
    currency: str
    availability: str
    color: str
    size: str
    image_url: str
    source_url: str
    content_hash: str
    collected_at: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def csv_row(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in CSV_FIELDS}

    def fingerprint_fields(self) -> dict[str, Any]:
        return {
            "availability": self.availability,
            "color": self.color,
            "currency": self.currency,
            "description": self.description,
            "image_url": self.image_url,
            "price_cents": self.price_cents,
            "size": self.size,
            "sku": self.sku,
            "title": self.title,
        }

    @classmethod
    def from_validated(cls, data: dict[str, Any]) -> Product:
        return cls(
            sku=data["sku"],
            title=data["title"],
            price_cents=int(data["price_cents"]),
            currency=data["currency"],
            availability=data["availability"],
            color=data["color"],
            size=data["size"],
            image_url=data["image_url"],
            source_url=data["source_url"],
            content_hash=data["content_hash"],
            collected_at=data["collected_at"],
            description=str(data.get("description") or ""),
        )


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class ChangeSet:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[tuple[str, tuple[FieldChange, ...]], ...]
    unchanged: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": {
                sku: [item.to_dict() for item in changes] for sku, changes in self.changed
            },
            "unchanged": list(self.unchanged),
        }


@dataclass
class Checkpoint:
    job_id: str
    phase: str = "robots"
    status: str = "in_progress"
    listing_pages_done: int = 0
    completed_paths: list[str] = field(default_factory=list)
    last_path: Optional[str] = None
    products_seen: int = 0
    rejected: int = 0
    updated_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "phase": self.phase,
            "status": self.status,
            "listing_pages_done": self.listing_pages_done,
            "completed_paths": list(self.completed_paths),
            "last_path": self.last_path,
            "products_seen": self.products_seen,
            "rejected": self.rejected,
            "updated_ms": self.updated_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        if not isinstance(data, dict):
            raise ValueError("checkpoint must be an object")
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("checkpoint.job_id is required")
        phase = str(data.get("phase") or "robots")
        status = str(data.get("status") or "in_progress")
        if phase not in CHECKPOINT_PHASES:
            raise ValueError(f"checkpoint.phase {phase!r} is invalid")
        if status not in CHECKPOINT_STATUSES:
            raise ValueError(f"checkpoint.status {status!r} is invalid")
        paths = data.get("completed_paths") or []
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            raise ValueError("checkpoint.completed_paths must be a list of strings")
        return cls(
            job_id=job_id,
            phase=phase,
            status=status,
            listing_pages_done=int(data.get("listing_pages_done") or 0),
            completed_paths=list(paths),
            last_path=data.get("last_path"),
            products_seen=int(data.get("products_seen") or 0),
            rejected=int(data.get("rejected") or 0),
            updated_ms=int(data.get("updated_ms") or 0),
        )


@dataclass
class CollectReport:
    job_id: str
    dry_run: bool
    resumed: bool
    listing_pages: int = 0
    products_fetched: int = 0
    products_not_modified: int = 0
    products_parsed: int = 0
    products_rejected: int = 0
    robots_skipped: int = 0
    retries: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    unchanged: int = 0
    csv_rows: int = 0
    rate_limit_wait_ms: int = 0
    collected_at: str = ""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "dry_run": self.dry_run,
            "resumed": self.resumed,
            "listing_pages": self.listing_pages,
            "products_fetched": self.products_fetched,
            "products_not_modified": self.products_not_modified,
            "products_parsed": self.products_parsed,
            "products_rejected": self.products_rejected,
            "robots_skipped": self.robots_skipped,
            "retries": self.retries,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "csv_rows": self.csv_rows,
            "rate_limit_wait_ms": self.rate_limit_wait_ms,
            "collected_at": self.collected_at,
            "checkpoint": dict(self.checkpoint),
            "changes": dict(self.changes),
        }
