"""Destination product snapshot, in-memory or durable JSON."""

from __future__ import annotations

from typing import Any, Optional
from pathlib import Path

from .errors import CheckpointError
from .models import FIXTURE_ORIGIN, Product
from .persist import atomic_write_json, read_json, safe_stem
from .schema import validate_snapshot


class CatalogStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self.origin = FIXTURE_ORIGIN
        self.collected_at = ""
        self.job_id = ""
        self.products: dict[str, Product] = {}
        self.etags: dict[str, str] = {}

    def load_from_snapshot(self, data: dict[str, Any]) -> None:
        parsed = validate_snapshot(data)
        self.origin = parsed["origin"]
        self.collected_at = parsed["collected_at"]
        self.job_id = parsed["job_id"]
        self.products = {item.sku: item for item in parsed["products"]}
        self.etags = dict(parsed["etags"])

    def load_file(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            payload = read_json(self.path)
        except (OSError, ValueError) as exc:
            raise CheckpointError(f"corrupt snapshot {self.path}: {exc}") from exc
        self.load_from_snapshot(payload)

    def upsert(self, product: Product) -> None:
        self.products[product.sku] = product

    def remember_etag(self, path: str, etag: str) -> None:
        self.etags[path] = etag

    def sorted_products(self) -> list[Product]:
        return [self.products[key] for key in sorted(self.products)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "collected_at": self.collected_at,
            "job_id": self.job_id,
            "products": [item.to_dict() for item in self.sorted_products()],
            "etags": dict(self.etags),
        }

    def persist(self) -> None:
        if self.path is None:
            return
        try:
            atomic_write_json(self.path, self.to_dict())
        except OSError as exc:
            raise CheckpointError(f"could not write snapshot {self.path}: {exc}") from exc


def snapshot_path_for(directory: Path, job_id: str) -> Path:
    return Path(directory) / f"{safe_stem(job_id)}.snapshot.json"
