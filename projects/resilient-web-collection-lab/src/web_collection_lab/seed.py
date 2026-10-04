"""Synthetic fixture catalog, robots.txt, previous snapshot, and faults."""

from __future__ import annotations

from typing import Any

from .models import FIXTURE_ORIGIN, LAB_NOW_MS, Product, ms_to_iso_z
from .persist import fingerprint

ROBOTS_TXT = """\
# Synthetic fixture robots. No live site is contacted.
User-agent: *
Disallow: /admin/
Crawl-delay: 0

User-agent: LabCollector
Allow: /catalog
Allow: /products/
Allow: /images/
Disallow: /private/
Crawl-delay: 1
"""

COLLECTED_AT = ms_to_iso_z(LAB_NOW_MS)
# The "last week" snapshot is stamped seven days before the lab clock.
PREVIOUS_COLLECTED_AT = ms_to_iso_z(LAB_NOW_MS - 7 * 24 * 60 * 60 * 1000)


def build_catalog() -> list[dict[str, Any]]:
    return [
        {
            "sku": "SKU-1001",
            "slug": "sku-1001",
            "title": "Linen Shirt",
            "title_html": "  Linen   Shirt ",
            "price_text": "$29.00",
            "price_cents": 2900,
            "currency": "USD",
            "availability": "in_stock",
            "color": "Navy",
            "size": "m",
            "image": "/images/sku-1001.jpg",
            "description": "Breathable & durable. Machine wash.",
            "description_html": "Breathable &amp; durable.&nbsp;Machine wash.",
            "charset": "utf-8",
            "data_cents": 2900,
        },
        {
            "sku": "SKU-1002",
            "slug": "sku-1002",
            "title": "Wool Coat, Lined",
            "title_html": "Wool Coat, Lined",
            "price_text": "$149.00",
            "price_cents": 14900,
            "currency": "USD",
            "availability": "in_stock",
            "color": "Charcoal",
            "size": "l",
            "image": "/images/sku-1002.jpg",
            "description": "Double-breasted winter coat.",
            "description_html": "Double-breasted winter coat.",
            "charset": "utf-8",
            "data_cents": None,
        },
        {
            "sku": "SKU-1003",
            "slug": "sku-1003",
            "title": "Café Apron",
            "title_html": "  Café   Apron ",
            "price_text": "24,50 EUR",
            "price_cents": 2450,
            "currency": "EUR",
            "availability": "in_stock",
            "color": "Natural",
            "size": "one",
            "image": "/images/sku-1003.jpg",
            "description": "Cotton canvas with a front pocket.",
            "description_html": "Cotton canvas with a front pocket.",
            "charset": "iso-8859-1",
            "data_cents": 2450,
        },
        {
            "sku": "SKU-1004",
            "slug": "sku-1004",
            "title": "Cotton Tee",
            "title_html": "Cotton Tee",
            "price_text": "¥2,400",
            "price_cents": 2400,
            "currency": "JPY",
            "availability": "out_of_stock",
            "color": "White",
            "size": "s",
            "image": "/images/sku-1004.jpg",
            "description": "Lightweight crew neck.",
            "description_html": "Lightweight crew neck.",
            "charset": "utf-8",
            "data_cents": None,
        },
        {
            "sku": "SKU-1005",
            "slug": "sku-1005",
            "title": "Denim Jacket",
            "title_html": "Denim Jacket",
            "price_text": "$89.00",
            "price_cents": 8900,
            "currency": "USD",
            "availability": "in_stock",
            "color": "Indigo",
            "size": "m",
            "image": "/images/sku-1005.jpg",
            "description": "Washed indigo denim.",
            "description_html": "Washed indigo denim.",
            "charset": "utf-8",
            "data_cents": 8900,
        },
        {
            "sku": "SKU-1006",
            "slug": "sku-1006",
            "title": "Silk Scarf",
            "title_html": "Silk Scarf",
            "price_text": "$45.00",
            "price_cents": 4500,
            "currency": "USD",
            "availability": "discontinued",
            "color": "Red",
            "size": "os",
            "image": "/images/sku-1006.jpg",
            "description": "Hand-rolled edges.",
            "description_html": "Hand-rolled edges.",
            "charset": "utf-8",
            "data_cents": None,
        },
    ]


def canonical_from_row(row: dict[str, Any], *, collected_at: str = COLLECTED_AT) -> Product:
    fields = {
        "availability": {
            "in_stock": "in_stock",
            "out_of_stock": "out_of_stock",
            "discontinued": "discontinued",
        }[row["availability"]],
        "color": str(row["color"]).strip().lower(),
        "currency": row["currency"],
        "description": row["description"],
        "image_url": f"{FIXTURE_ORIGIN}{row['image']}",
        "price_cents": int(row["price_cents"]),
        "size": str(row["size"]).strip().upper(),
        "sku": row["sku"],
        "title": row["title"],
    }
    digest = fingerprint(fields)
    return Product(
        sku=fields["sku"],
        title=fields["title"],
        price_cents=fields["price_cents"],
        currency=fields["currency"],
        availability=fields["availability"],
        color=fields["color"],
        size=fields["size"],
        image_url=fields["image_url"],
        source_url=f"{FIXTURE_ORIGIN}/products/{row['slug']}",
        content_hash=digest,
        collected_at=collected_at,
        description=fields["description"],
    )


def expected_products(*, collected_at: str = COLLECTED_AT) -> list[Product]:
    return [canonical_from_row(row, collected_at=collected_at) for row in build_catalog()]


def _retired_product(*, collected_at: str = COLLECTED_AT) -> Product:
    fields = {
        "availability": "discontinued",
        "color": "olive",
        "currency": "USD",
        "description": "No longer listed.",
        "image_url": f"{FIXTURE_ORIGIN}/images/sku-old1.jpg",
        "price_cents": 9900,
        "size": "L",
        "sku": "SKU-OLD1",
        "title": "Retired Parka",
    }
    digest = fingerprint(fields)
    return Product(
        sku="SKU-OLD1",
        title="Retired Parka",
        price_cents=9900,
        currency="USD",
        availability="discontinued",
        color="olive",
        size="L",
        image_url=fields["image_url"],
        source_url=f"{FIXTURE_ORIGIN}/products/sku-old1",
        content_hash=digest,
        collected_at=collected_at,
        description=fields["description"],
    )


def build_previous_snapshot() -> dict[str, Any]:
    stamp = PREVIOUS_COLLECTED_AT
    by_sku = {item.sku: item for item in expected_products(collected_at=stamp)}
    older = canonical_from_row(
        {**next(row for row in build_catalog() if row["sku"] == "SKU-1001"), "price_cents": 2800},
        collected_at=stamp,
    )
    products = [
        older,
        by_sku["SKU-1002"],
        by_sku["SKU-1005"],
        _retired_product(collected_at=stamp),
    ]
    return {
        "origin": FIXTURE_ORIGIN,
        "collected_at": stamp,
        "job_id": "lab-collect-previous",
        "products": [item.to_dict() for item in products],
        "etags": {},
    }


def build_fault_script() -> list[dict[str, Any]]:
    return [
        {"method": "GET", "path": "/robots.txt", "status": 503},
        {"method": "GET", "path": "/catalog", "status": 429, "headers": {"Retry-After": "0"}},
        {"method": "GET", "path": "/products/sku-1001", "timeout": True},
    ]


def build_robots_txt() -> str:
    return ROBOTS_TXT
