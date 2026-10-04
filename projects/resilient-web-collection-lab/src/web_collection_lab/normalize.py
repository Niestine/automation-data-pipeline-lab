"""Field coercions: SKU, whitespace, money, availability, URLs."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Optional
from urllib.parse import urljoin, urlparse
import re

from .errors import SchemaError
from .models import (
    AVAILABILITIES,
    COLOR_MAX,
    CURRENCIES,
    CURRENCY_DECIMALS,
    DESCRIPTION_MAX,
    FIXTURE_ORIGIN,
    SIZE_MAX,
    SKU_PATTERN,
    TITLE_MAX,
    Product,
)
from .parse import ParsedProduct
from .persist import fingerprint


_SKU_RE = re.compile(SKU_PATTERN)
_SYMBOLS = {"$": "USD", "€": "EUR", "¥": "JPY", "￥": "JPY"}
_CURRENCY_CODES = {code.lower(): code for code in CURRENCIES}
_MAX_AMOUNT = Decimal("100000000")
# Plain digits with an optional fraction. Keeps Decimal from accepting
# "NaN", "Infinity", or exponent forms such as "1e3".
_PLAIN_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?")

_AVAIL_MAP = {
    "in_stock": "in_stock",
    "in-stock": "in_stock",
    "instock": "in_stock",
    "true": "in_stock",
    "yes": "in_stock",
    "1": "in_stock",
    "out_of_stock": "out_of_stock",
    "out-of-stock": "out_of_stock",
    "outofstock": "out_of_stock",
    "false": "out_of_stock",
    "no": "out_of_stock",
    "0": "out_of_stock",
    "discontinued": "discontinued",
    "retired": "discontinued",
    "dropped": "discontinued",
}


def collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


def normalize_sku(raw: str) -> str:
    sku = collapse_ws(raw).upper().replace(" ", "")
    if not _SKU_RE.fullmatch(sku):
        raise SchemaError(f"invalid sku {raw!r}", field="sku")
    return sku


def normalize_title(raw: str) -> str:
    title = collapse_ws(raw)
    if not title:
        raise SchemaError("empty title", field="title")
    if len(title) > TITLE_MAX:
        raise SchemaError("title too long", field="title")
    return title


def normalize_color(raw: str) -> str:
    color = collapse_ws(raw).lower()
    if not color:
        raise SchemaError("empty color", field="color")
    if len(color) > COLOR_MAX:
        raise SchemaError("color too long", field="color")
    return color


def normalize_size(raw: str) -> str:
    size = collapse_ws(raw).upper()
    if not size:
        raise SchemaError("empty size", field="size")
    if len(size) > SIZE_MAX:
        raise SchemaError("size too long", field="size")
    return size


def normalize_availability(raw: str) -> str:
    key = collapse_ws(raw).lower().replace(" ", "_")
    mapped = _AVAIL_MAP.get(key)
    if mapped is None or mapped not in AVAILABILITIES:
        raise SchemaError(f"invalid availability {raw!r}", field="availability")
    return mapped


def normalize_description(raw: str) -> str:
    text = collapse_ws(raw)
    if len(text) > DESCRIPTION_MAX:
        raise SchemaError("description too long", field="description")
    return text


def normalize_image_url(raw: str, *, base: str) -> str:
    url = collapse_ws(raw)
    if not url:
        raise SchemaError("empty image url", field="image_url")
    absolute = urljoin(base, url)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        raise SchemaError(f"image url must be http(s): {url!r}", field="image_url")
    if not parsed.netloc:
        raise SchemaError(f"image url missing host: {url!r}", field="image_url")
    return absolute


def detect_currency(price_text: str, hint: str = "") -> str:
    hint = collapse_ws(hint).upper()
    if hint in CURRENCIES:
        return hint
    text = price_text or ""
    for symbol, code in _SYMBOLS.items():
        if symbol in text:
            return code
    for token in re.findall(r"[A-Za-z]{3}", text):
        mapped = _CURRENCY_CODES.get(token.lower())
        if mapped:
            return mapped
    raise SchemaError(f"could not detect currency from {price_text!r}", field="currency")


def parse_price(raw: str, currency: str) -> int:
    if currency not in CURRENCIES:
        raise SchemaError(f"unknown currency {currency}", field="currency")
    text = collapse_ws(raw)
    if not text:
        raise SchemaError("empty price", field="price")
    for symbol in _SYMBOLS:
        text = text.replace(symbol, "")
    text = re.sub(r"(USD|EUR|JPY)", "", text, flags=re.I)
    text = collapse_ws(text).replace(" ", "")
    if not text:
        raise SchemaError(f"invalid price {raw!r}", field="price")
    if text.startswith("-"):
        raise SchemaError("negative price", field="price")
    text = _normalize_separators(text, currency)
    if _PLAIN_AMOUNT.fullmatch(text) is None:
        raise SchemaError(f"invalid price {raw!r}", field="price")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise SchemaError(f"invalid price {raw!r}", field="price") from exc
    if amount > _MAX_AMOUNT:
        raise SchemaError("price too large", field="price")
    decimals = CURRENCY_DECIMALS[currency]
    quant = Decimal("1").scaleb(-decimals)
    quantized = amount.quantize(quant, rounding=ROUND_HALF_EVEN)
    if quantized != amount:
        raise SchemaError(f"{currency} price has too many decimal places", field="price")
    minor = quantized * (Decimal(10) ** decimals)
    return int(minor)


def _normalize_separators(text: str, currency: str) -> str:
    if currency == "JPY":
        if "." in text:
            raise SchemaError("JPY price must be a whole number", field="price")
        return text.replace(",", "")
    if currency == "EUR" and "," in text:
        if "." in text and text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", ".")
        if "." not in text:
            return text.replace(",", ".")
        raise SchemaError("ambiguous EUR price separators", field="price")
    if "," in text and "." in text:
        if text.rfind(".") > text.rfind(","):
            return text.replace(",", "")
        raise SchemaError("ambiguous thousands separator", field="price")
    if "," in text:
        raise SchemaError(
            "comma in a non-EUR price requires thousands grouping with a decimal point",
            field="price",
        )
    return text


def same_origin_path(href: str, *, base: str, origin: str = FIXTURE_ORIGIN) -> Optional[tuple[str, dict[str, str]]]:
    """Resolve href against base and return (path, query) when same-origin."""
    from urllib.parse import parse_qsl

    absolute = urljoin(base, href)
    parsed = urlparse(absolute)
    origin_parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc != origin_parsed.netloc or parsed.scheme != origin_parsed.scheme:
        return None
    path = parsed.path or "/"
    query = {str(k): str(v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
    return path, query


def normalize_product(
    parsed: ParsedProduct,
    *,
    source_url: str,
    collected_at: str,
    origin: str = FIXTURE_ORIGIN,
) -> Product:
    sku = normalize_sku(parsed.sku)
    title = normalize_title(parsed.title)
    currency = detect_currency(parsed.price_text, parsed.currency)
    price_cents = parse_price(parsed.price_text, currency)
    if parsed.data_cents is not None and parsed.data_cents != price_cents:
        raise SchemaError(
            f"data-cents {parsed.data_cents} does not match display price {price_cents}",
            field="price",
        )
    image_url = normalize_image_url(parsed.image, base=source_url)
    parsed_image = urlparse(image_url)
    origin_host = urlparse(origin).netloc
    if parsed_image.netloc != origin_host:
        raise SchemaError("image url is off-origin", field="image_url")
    fields = {
        "sku": sku,
        "title": title,
        "price_cents": price_cents,
        "currency": currency,
        "availability": normalize_availability(parsed.availability),
        "color": normalize_color(parsed.color),
        "size": normalize_size(parsed.size),
        "image_url": image_url,
        "description": normalize_description(parsed.description),
    }
    digest = fingerprint(fields)
    return Product(
        sku=sku,
        title=title,
        price_cents=price_cents,
        currency=currency,
        availability=fields["availability"],
        color=fields["color"],
        size=fields["size"],
        image_url=image_url,
        source_url=source_url,
        content_hash=digest,
        collected_at=collected_at,
        description=fields["description"],
    )
