"""Domain objects for the catalog maintenance lab."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# 2026-01-01T00:00:00Z. Catalog fixtures are dated before this instant.
LAB_EPOCH_MS = 1_767_225_600_000
# 2026-01-04T12:00:00Z (Sunday, ISO week 2026-W01). Default CLI/test clock.
LAB_NOW_MS = LAB_EPOCH_MS + 3 * 86_400_000 + 12 * 3_600_000

CURRENCIES = ("USD", "EUR", "JPY")
CURRENCY_DECIMALS = {"USD": 2, "EUR": 2, "JPY": 0}
FEED_VERSIONS = ("v1", "v2")
APPLY_STATUSES = (
    "inserted",
    "updated",
    "replayed",
    "stale",
    "conflict",
    "rejected",
)
SKU_PATTERN = r"^[A-Z0-9][A-Z0-9-]{2,31}$"
ISO_DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
ISO_Z = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
WEEK_ID_PATTERN = r"^[0-9]{4}-W[0-9]{2}$"
PREAMBLE_KEYS = ("encoding", "version", "currency", "delimiter", "format")
# Upper bounds shared by row validation and the catalog file schema, so an
# accepted row can always be reloaded from disk.
MAX_PRICE_MINOR = 10_000_000_000
MAX_IMAGE_URL_LENGTH = 500
# Marker in RawRow.extra_columns for a CSV record with more cells than the header.
TRAILING_CELLS = "__trailing__"

TRUE_TOKENS = frozenset({"true", "1", "yes", "y", "on"})
FALSE_TOKENS = frozenset({"false", "0", "no", "n", "off"})

HEADER_ALIASES = {
    "sku": "sku",
    "product_name": "product_name",
    "title": "title",
    "price": "price",
    "price_cents": "price_cents",
    "qty": "qty",
    "quantity": "qty",
    "stock": "stock",
    "active": "active",
    "image": "image",
    "image_url": "image_url",
    "updated_at": "updated_at",
    "currency": "currency",
}


@dataclass(frozen=True)
class Product:
    sku: str
    title: str
    price_cents: int
    currency: str
    stock: int
    active: bool
    image_url: Optional[str]
    version: int
    updated_at_ms: int
    source_version: str
    fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_validated(cls, data: dict[str, Any], fingerprint: str) -> Product:
        image = data.get("image_url")
        return cls(
            sku=str(data["sku"]),
            title=str(data["title"]),
            price_cents=int(data["price_cents"]),
            currency=str(data["currency"]),
            stock=int(data["stock"]),
            active=bool(data["active"]),
            image_url=None if image in (None, "") else str(image),
            version=int(data["version"]),
            updated_at_ms=int(data["updated_at_ms"]),
            source_version=str(data["source_version"]),
            fingerprint=fingerprint,
        )


@dataclass(frozen=True)
class DecodeResult:
    text: str
    encoding: str
    bug_guards: tuple[str, ...] = ()


@dataclass(frozen=True)
class RawRow:
    index: int
    line_num: Optional[int]
    fields: dict[str, str]
    origins: dict[str, str]
    format: str
    version: str
    decimal_comma: bool
    currency_hint: Optional[str]
    extra_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanonicalRecord:
    sku: str
    title: str
    price_cents: int
    currency: str
    stock: Optional[int]
    active: Optional[bool]
    image_url: Optional[str]
    updated_at_ms: int
    source_version: str
    row_index: int
    remaps: tuple[str, ...]
    bug_guards: tuple[str, ...]


@dataclass(frozen=True)
class RowOutcome:
    index: int
    sku: Optional[str]
    status: str
    code: str
    message: str
    remaps: tuple[str, ...] = ()
    bug_guards: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "sku": self.sku,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "remaps": list(self.remaps),
            "bug_guards": list(self.bug_guards),
        }


@dataclass(frozen=True)
class FeedReport:
    name: str
    encoding: str
    format: str
    version: str
    sha256: str
    week_id: str
    replayed_feed: bool
    outcomes: tuple[RowOutcome, ...] = ()

    def counts(self) -> dict[str, int]:
        tallies = {status: 0 for status in APPLY_STATUSES}
        for outcome in self.outcomes:
            if outcome.status in tallies:
                tallies[outcome.status] += 1
        tallies["rows"] = len(self.outcomes)
        return tallies

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "encoding": self.encoding,
            "format": self.format,
            "version": self.version,
            "sha256": self.sha256,
            "week_id": self.week_id,
            "replayed_feed": self.replayed_feed,
            "counts": self.counts(),
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


@dataclass
class RunReport:
    week_id: str
    dry_run: bool
    feeds: list[FeedReport] = field(default_factory=list)
    catalog_size: int = 0
    crash_after_rows: Optional[int] = None

    def all_outcomes(self) -> list[RowOutcome]:
        rows: list[RowOutcome] = []
        for feed in self.feeds:
            rows.extend(feed.outcomes)
        return rows

    def counts(self) -> dict[str, int]:
        tallies = {status: 0 for status in APPLY_STATUSES}
        tallies["feed_replays"] = 0
        tallies["rows"] = 0
        for feed in self.feeds:
            if feed.replayed_feed:
                tallies["feed_replays"] += 1
            for status, value in feed.counts().items():
                if status == "rows":
                    tallies["rows"] += value
                else:
                    tallies[status] += value
        return tallies

    def bug_guards(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for outcome in self.all_outcomes():
            for bug_id in outcome.bug_guards:
                if bug_id not in seen:
                    seen.add(bug_id)
                    ordered.append(bug_id)
        return ordered

    def to_dict(self) -> dict[str, Any]:
        return {
            "week_id": self.week_id,
            "dry_run": self.dry_run,
            "catalog_size": self.catalog_size,
            "counts": self.counts(),
            "bug_guards": self.bug_guards(),
            "feeds": [item.to_dict() for item in self.feeds],
        }


@dataclass(frozen=True)
class FeedInput:
    name: str
    data: bytes
    declared_encoding: Optional[str] = None
    declared_version: Optional[str] = None
    declared_currency: Optional[str] = None


@dataclass(frozen=True)
class Checkpoint:
    week_id: str
    feed_name: str
    feed_sha256: str
    last_row: int
    status: str
    applied_skus: tuple[str, ...] = ()
    rejected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "week_id": self.week_id,
            "feed_name": self.feed_name,
            "feed_sha256": self.feed_sha256,
            "last_row": self.last_row,
            "status": self.status,
            "applied_skus": list(self.applied_skus),
            "rejected": self.rejected,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(
            week_id=str(data["week_id"]),
            feed_name=str(data["feed_name"]),
            feed_sha256=str(data["feed_sha256"]),
            last_row=int(data["last_row"]),
            status=str(data["status"]),
            applied_skus=tuple(str(item) for item in data.get("applied_skus") or []),
            rejected=int(data.get("rejected") or 0),
        )


@dataclass(frozen=True)
class CompatChange:
    sku: str
    field: str
    before: Any
    after: Any
    breaking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompatReport:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[CompatChange, ...]

    @property
    def breaking(self) -> tuple[CompatChange, ...]:
        removed_changes = tuple(
            CompatChange(sku=sku, field="sku", before=sku, after=None, breaking=True)
            for sku in self.removed
        )
        field_breaks = tuple(item for item in self.changed if item.breaking)
        return removed_changes + field_breaks

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": [item.to_dict() for item in self.changed],
            "breaking": [item.to_dict() for item in self.breaking],
        }


@dataclass(frozen=True)
class Bug:
    id: str
    title: str
    layer: str
    symptom: str
    fix: str
    regression_test: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
