"""Versioned SKU catalog with optional durable JSON and dry-run overlay."""

from __future__ import annotations

from typing import Optional
import json
from pathlib import Path

from .errors import SchemaError, StateError
from .models import Product
from .persist import atomic_write_json, fingerprint_fields, read_json
from .schema import load_catalog_payload


class CatalogStore:
    def __init__(self, path: str | Path | None = None, products: Optional[list[Product]] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._items: dict[str, Product] = {}
        self._dry = False
        self._backup: Optional[dict[str, Product]] = None
        if products is not None:
            for product in products:
                self._items[product.sku] = product
            self._persist()
        elif self.path is not None and self.path.exists():
            self._items = _load_path(self.path)

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> dict[str, Product]:
        return dict(self._items)

    def get(self, sku: str) -> Optional[Product]:
        return self._items.get(sku)

    def put(self, product: Product) -> None:
        self._items[product.sku] = product
        self._persist()

    def products(self) -> list[Product]:
        return [self._items[key] for key in sorted(self._items)]

    def begin_dry_run(self) -> None:
        if self._dry:
            return
        self._backup = dict(self._items)
        self._dry = True

    def abort_dry_run(self) -> None:
        if not self._dry:
            return
        if self._backup is not None:
            self._items = self._backup
        self._backup = None
        self._dry = False

    def _persist(self) -> None:
        if self._dry or self.path is None:
            return
        payload = {"products": [item.to_dict() for item in self.products()]}
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:
            raise StateError(f"could not write catalog {self.path}: {exc}") from exc


def make_product(
    *,
    sku: str,
    title: str,
    price_cents: int,
    currency: str,
    stock: int,
    active: bool,
    image_url: Optional[str],
    version: int,
    updated_at_ms: int,
    source_version: str,
) -> Product:
    fingerprint = fingerprint_fields(
        sku, title, price_cents, currency, stock, active, image_url
    )
    return Product(
        sku=sku,
        title=title,
        price_cents=price_cents,
        currency=currency,
        stock=stock,
        active=active,
        image_url=image_url,
        version=version,
        updated_at_ms=updated_at_ms,
        source_version=source_version,
        fingerprint=fingerprint,
    )


def _load_path(path: Path) -> dict[str, Product]:
    try:
        data = read_json(path)
        rows = load_catalog_payload(data)
    except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError, SchemaError) as exc:
        raise StateError(f"corrupt catalog {path}: {exc}") from exc
    items: dict[str, Product] = {}
    for row in rows:
        product = Product.from_validated(row, row["fingerprint"])
        items[product.sku] = product
    return items
