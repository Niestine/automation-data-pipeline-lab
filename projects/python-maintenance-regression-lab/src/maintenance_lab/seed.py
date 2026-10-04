"""Synthetic catalog and weekly supplier feeds."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog import make_product
from .models import FeedInput, Product
from .window import ms_from_utc


def ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return ms_from_utc(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc))


CATALOG_STAMP_MS = ms(2025, 12, 15)


def build_products() -> list[Product]:
    return [
        make_product(
            sku="SKU-1001",
            title="Widget Alpha",
            price_cents=1999,
            currency="USD",
            stock=10,
            active=True,
            image_url="https://cdn.example.test/sku-1001.png",
            version=1,
            updated_at_ms=CATALOG_STAMP_MS,
            source_version="v2",
        ),
        make_product(
            sku="SKU-1002",
            title="Widget Beta",
            price_cents=2500,
            currency="USD",
            stock=5,
            active=True,
            image_url="https://cdn.example.test/sku-1002.png",
            version=1,
            updated_at_ms=CATALOG_STAMP_MS,
            source_version="v2",
        ),
        make_product(
            sku="JP-2001",
            title="木綿シャツ",
            price_cents=4500,
            currency="JPY",
            stock=3,
            active=True,
            image_url="https://cdn.example.test/jp-2001.png",
            version=1,
            updated_at_ms=CATALOG_STAMP_MS,
            source_version="v1",
        ),
        make_product(
            sku="EU-3001",
            title="Linen Shirt",
            price_cents=1299,
            currency="EUR",
            stock=8,
            active=True,
            image_url="https://cdn.example.test/eu-3001.png",
            version=1,
            updated_at_ms=CATALOG_STAMP_MS,
            source_version="v1",
        ),
    ]


def catalog_payload(products: list[Product] | None = None) -> dict[str, Any]:
    rows = products if products is not None else build_products()
    return {"products": [item.to_dict() for item in rows]}


V1_CSV = """# version=v1 currency=USD
sku,product_name,price,qty,active,image,updated_at,notes
SKU-1001,Widget Alpha,21.50,12,true,https://cdn.example.test/sku-1001.png,2026-01-02T00:00:00Z,price bump
SKU-1002,Widget Beta,25.00,,true,https://cdn.example.test/sku-1002.png,2026-01-02T00:00:00Z,qty omitted
SKU-1003,"Widget, Gamma",9.99,4,yes,https://cdn.example.test/sku-1003.png,2026-01-02,quoted comma
sku-1003,WIDGET GAMMA DUP,9.99,4,true,https://cdn.example.test/sku-1003.png,2026-01-02,case-fold conflict
SKU-1004,Widget Delta,5.00,0,false,https://cdn.example.test/sku-1004.png,2026-01-03,discontinued
XX,too short,1.00,1,true,https://cdn.example.test/x.png,2026-01-02,invalid sku
SKU-1005,Bad Image,3.00,1,true,./relative.png,2026-01-02,relative image
SKU-1006,US Date,4.00,1,true,https://cdn.example.test/x.png,01/07/2026,slash date
SKU-1007,Truthy False,8.00,2,false,https://cdn.example.test/x.png,2026-01-02,string false
"""

JP_CSV = """# version=v1 currency=JPY encoding=cp932
sku,product_name,price,qty,active,image,updated_at
JP-2001,木綿シャツ,4800,3,true,https://cdn.example.test/jp-2001.png,2026-01-02
JP-2002,絹のスカーフ,2200,,true,https://cdn.example.test/jp-2002.png,2026-01-02
"""

EU_CSV = """# version=v1 currency=EUR delimiter=;
sku;product_name;price;qty;active;image;updated_at
EU-3001;Linen Shirt;14,90;6;true;https://cdn.example.test/eu-3001.png;2026-01-02
EU-3002;Wool Coat;199,00;1;true;https://cdn.example.test/eu-3002.png;2026-01-02
"""

V2_JSON = """{
  "version": "v2",
  "currency": "USD",
  "items": [
    {
      "sku": "SKU-1001",
      "title": "Widget Alpha",
      "price_cents": 2150,
      "stock": 12,
      "active": true,
      "image_url": "https://cdn.example.test/sku-1001.png",
      "updated_at": "2026-01-02T00:00:00Z"
    },
    {
      "sku": "SKU-2001",
      "title": "Nexus Hub",
      "price": "40.00",
      "currency": "USD",
      "stock": 7,
      "active": true,
      "image_url": "https://cdn.example.test/sku-2001.png",
      "updated_at": "2026-01-03T12:00:00Z"
    }
  ]
}
"""

LATIN1_CSV = """# version=v1 currency=EUR delimiter=;
sku;product_name;price;qty;active;image;updated_at
EU-4001;Café Linen;12,50;2;true;https://cdn.example.test/eu-4001.png;2026-01-02
"""

NEWLINE_CSV = """# version=v1 currency=USD
sku,product_name,price,qty,active,image,updated_at
SKU-9001,"Line one
Line two",11.00,1,true,https://cdn.example.test/sku-9001.png,2026-01-02
"""


def jp_cp932_bytes() -> bytes:
    return JP_CSV.encode("cp932")


def latin1_bytes() -> bytes:
    return LATIN1_CSV.encode("latin-1")


def build_default_feeds() -> list[FeedInput]:
    return [
        FeedInput(name="supplier_v1.csv", data=V1_CSV.encode("utf-8")),
        FeedInput(name="supplier_v1_jp.cp932", data=jp_cp932_bytes()),
        FeedInput(name="supplier_eu.csv", data=EU_CSV.encode("utf-8")),
        FeedInput(name="supplier_v2.json", data=V2_JSON.encode("utf-8")),
        FeedInput(name="supplier_latin1.csv", data=latin1_bytes()),
        FeedInput(name="supplier_newline.csv", data=NEWLINE_CSV.encode("utf-8")),
    ]
