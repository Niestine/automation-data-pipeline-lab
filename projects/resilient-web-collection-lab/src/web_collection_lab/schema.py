"""Canonical product and snapshot contracts."""

from __future__ import annotations

from typing import Any
import re

from .errors import SchemaError
from .models import (
    AVAILABILITIES,
    COLOR_MAX,
    CURRENCIES,
    DESCRIPTION_MAX,
    SIZE_MAX,
    SKU_PATTERN,
    TITLE_MAX,
    Product,
)
from .persist import fingerprint

_SKU_RE = re.compile(SKU_PATTERN)
_ISO_Z = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_HTTP_URL = re.compile(r"^https?://[^/\s]+/\S*$")


def require_object(data: Any, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError(f"{label} must be an object")
    return data


def validate_product_dict(data: Any) -> Product:
    row = require_object(data, "product")
    errors: list[str] = []

    def field(name: str) -> Any:
        return row.get(name)

    sku = field("sku")
    if not isinstance(sku, str) or not _SKU_RE.fullmatch(sku):
        errors.append("sku")
    title = field("title")
    if not isinstance(title, str) or not title or len(title) > TITLE_MAX or title != title.strip():
        errors.append("title")
    price = field("price_cents")
    if isinstance(price, bool) or not isinstance(price, int) or price < 0 or price > 100_000_000:
        errors.append("price_cents")
    currency = field("currency")
    if currency not in CURRENCIES:
        errors.append("currency")
    availability = field("availability")
    if availability not in AVAILABILITIES:
        errors.append("availability")
    color = field("color")
    if not isinstance(color, str) or not color or len(color) > COLOR_MAX or color != color.lower():
        errors.append("color")
    size = field("size")
    if not isinstance(size, str) or not size or len(size) > SIZE_MAX or size != size.upper():
        errors.append("size")
    image_url = field("image_url")
    if not isinstance(image_url, str) or _HTTP_URL.fullmatch(image_url) is None:
        errors.append("image_url")
    source_url = field("source_url")
    if not isinstance(source_url, str) or _HTTP_URL.fullmatch(source_url) is None:
        errors.append("source_url")
    collected_at = field("collected_at")
    if not isinstance(collected_at, str) or _ISO_Z.fullmatch(collected_at) is None:
        errors.append("collected_at")
    description = field("description") if "description" in row else ""
    if not isinstance(description, str) or len(description) > DESCRIPTION_MAX:
        errors.append("description")
    digest = field("content_hash")
    if not isinstance(digest, str) or _HASH.fullmatch(digest) is None:
        errors.append("content_hash")
    if errors:
        raise SchemaError("invalid product: " + ", ".join(errors), errors=errors)

    extra = set(row) - {
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
        "description",
    }
    if extra:
        raise SchemaError("unexpected product fields: " + ", ".join(sorted(extra)))

    product = Product.from_validated({**row, "description": description})
    expected = fingerprint(product.fingerprint_fields())
    if product.content_hash != expected:
        raise SchemaError("content_hash does not match canonical fields", field="content_hash")
    return product


def validate_snapshot(data: Any) -> dict[str, Any]:
    payload = require_object(data, "snapshot")
    origin = payload.get("origin")
    if not isinstance(origin, str) or not origin:
        raise SchemaError("snapshot.origin is required")
    collected_at = payload.get("collected_at")
    if not isinstance(collected_at, str) or _ISO_Z.fullmatch(collected_at) is None:
        raise SchemaError("snapshot.collected_at must be ISO-8601 UTC")
    products = payload.get("products")
    if not isinstance(products, list):
        raise SchemaError("snapshot.products must be an array")
    parsed: list[Product] = []
    seen: set[str] = set()
    for index, row in enumerate(products):
        product = validate_product_dict(row)
        if product.sku in seen:
            raise SchemaError(f"duplicate sku {product.sku} at products[{index}]")
        seen.add(product.sku)
        parsed.append(product)
    etags = payload.get("etags") or {}
    if not isinstance(etags, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in etags.items()):
        raise SchemaError("snapshot.etags must be an object of string to string")
    return {
        "origin": origin,
        "collected_at": collected_at,
        "job_id": str(payload.get("job_id") or ""),
        "products": parsed,
        "etags": dict(etags),
    }
