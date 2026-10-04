"""v1/v2 field adapters, defensive coercions, and catalog compatibility diffs."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse
import re

from .errors import ValidationError
from .models import (
    CURRENCIES,
    FALSE_TOKENS,
    MAX_IMAGE_URL_LENGTH,
    MAX_PRICE_MINOR,
    SKU_PATTERN,
    TRAILING_CELLS,
    TRUE_TOKENS,
    CanonicalRecord,
    CompatChange,
    CompatReport,
    Product,
    RawRow,
)
from .money import parse_price
from .window import parse_iso_datetime

_SKU_RE = re.compile(SKU_PATTERN)
_WS_RE = re.compile(r"\s+")


def normalize_sku(raw: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    remaps: list[str] = []
    guards: list[str] = []
    stripped = raw.strip()
    if stripped != raw:
        remaps.append("sku_strip")
    if not stripped:
        raise ValidationError("empty sku", field="sku")
    upper = stripped.upper()
    if upper != stripped:
        remaps.append("sku_casefold")
        guards.append("BUG-006")
    if len(upper) > 32:
        raise ValidationError("sku exceeds 32 characters", field="sku")
    if len(upper) > 12:
        guards.append("BUG-008")
    if _SKU_RE.fullmatch(upper) is None:
        raise ValidationError(f"invalid sku {raw!r}", field="sku")
    return upper, tuple(remaps), tuple(guards)


def parse_active(raw: Optional[str], *, origin: str) -> tuple[Optional[bool], tuple[str, ...]]:
    if raw is None:
        return None, ()
    text = raw.strip()
    if text == "":
        return None, ()
    if origin == "bool":
        return text == "true", ()
    low = text.lower()
    if low in TRUE_TOKENS:
        return True, ()
    if low in FALSE_TOKENS:
        return False, ("BUG-007",)
    raise ValidationError(f"invalid active {raw!r}", field="active")


def parse_stock(raw: Optional[str], *, present: bool) -> tuple[Optional[int], tuple[str, ...]]:
    if not present:
        return None, ()
    if raw is None:
        return None, ("BUG-005",)
    text = raw.strip()
    if text == "":
        return None, ("BUG-005",)
    if not re.fullmatch(r"0|[1-9][0-9]{0,8}", text):
        raise ValidationError(f"invalid stock {raw!r}", field="stock")
    return int(text), ()


def normalize_title(raw: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    remaps: list[str] = []
    guards: list[str] = []
    if "\n" in raw or "\r" in raw:
        guards.append("BUG-011")
        remaps.append("title_newline_collapsed")
        raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    stripped = raw.strip()
    if stripped != raw:
        remaps.append("title_strip")
    collapsed = _WS_RE.sub(" ", stripped)
    if collapsed != stripped:
        remaps.append("title_whitespace")
    if not collapsed:
        raise ValidationError("empty title", field="title")
    if len(collapsed) > 200:
        raise ValidationError("title exceeds 200 characters", field="title")
    return collapsed, tuple(remaps), tuple(guards)


def normalize_image(raw: Optional[str], *, present: bool) -> tuple[Optional[str], tuple[str, ...], tuple[str, ...]]:
    if not present:
        return None, (), ()
    if raw is None:
        return None, (), ()
    stripped = raw.strip()
    remaps: list[str] = []
    guards: list[str] = []
    if stripped != raw:
        remaps.append("image_strip")
        guards.append("BUG-013")
    if stripped == "":
        return None, tuple(remaps), tuple(guards)
    if len(stripped) > MAX_IMAGE_URL_LENGTH:
        raise ValidationError(
            f"image_url exceeds {MAX_IMAGE_URL_LENGTH} characters",
            field="image_url",
        )
    parsed = urlparse(stripped)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(
            "image_url must be an absolute http(s) URL",
            field="image_url",
            bug_guards=("BUG-013",),
        )
    return stripped, tuple(remaps), tuple(guards)


def _first_present(fields: dict[str, str], *names: str) -> tuple[Optional[str], Optional[str]]:
    for name in names:
        if name in fields:
            return fields[name], name
    return None, None


def adapt_row(row: RawRow) -> CanonicalRecord:
    remaps: list[str] = []
    guards: list[str] = []
    fields = row.fields
    origins = row.origins
    if TRAILING_CELLS in row.extra_columns:
        raise ValidationError(
            "row has more cells than the header (unquoted delimiter?)",
            bug_guards=("BUG-002",),
        )

    sku_raw, _ = _first_present(fields, "sku")
    if sku_raw is None:
        raise ValidationError("missing sku", field="sku")
    sku, sku_remaps, sku_guards = normalize_sku(sku_raw)
    remaps.extend(sku_remaps)
    guards.extend(sku_guards)

    title_raw, title_key = _first_present(fields, "title", "product_name")
    if title_raw is None:
        raise ValidationError("missing title", field="title")
    if title_key == "product_name":
        remaps.append("product_name->title")
    title, title_remaps, title_guards = normalize_title(title_raw)
    remaps.extend(title_remaps)
    guards.extend(title_guards)
    if row.decimal_comma is False and "," in title_raw and row.format == "csv":
        guards.append("BUG-002")

    currency_raw, _ = _first_present(fields, "currency")
    currency = (currency_raw or row.currency_hint or "").strip()
    if not currency:
        raise ValidationError("missing currency", field="currency")
    if currency not in CURRENCIES:
        raise ValidationError(f"unknown currency {currency}", field="currency")

    price_cents_raw, price_cents_key = _first_present(fields, "price_cents")
    price_raw, price_key = _first_present(fields, "price")
    if price_cents_raw is not None and price_cents_raw.strip() != "":
        if not re.fullmatch(r"0|[1-9][0-9]{0,10}", price_cents_raw.strip()):
            raise ValidationError(f"invalid price_cents {price_cents_raw!r}", field="price")
        price_cents = int(price_cents_raw.strip())
        if price_cents > MAX_PRICE_MINOR:
            raise ValidationError("price_cents too large", field="price")
        if price_cents_key:
            remaps.append("price_cents")
    elif price_raw not in (None, ""):
        if row.decimal_comma:
            guards.append("BUG-012")
        guards.append("BUG-003")
        price_cents = parse_price(price_raw, currency, decimal_comma=row.decimal_comma)
        if price_key:
            remaps.append("price->price_cents")
    else:
        raise ValidationError("missing price", field="price")

    stock_raw, stock_key = _first_present(fields, "stock", "qty")
    stock_present = stock_key is not None
    if stock_key == "qty":
        remaps.append("qty->stock")
    stock, stock_guards = parse_stock(stock_raw, present=stock_present)
    guards.extend(stock_guards)

    active_raw, _ = _first_present(fields, "active")
    active, active_guards = parse_active(active_raw, origin=origins.get("active", "str"))
    guards.extend(active_guards)

    image_raw, image_key = _first_present(fields, "image_url", "image")
    image_present = image_key is not None
    if image_key == "image":
        remaps.append("image->image_url")
    image_url, image_remaps, image_guards = normalize_image(image_raw, present=image_present)
    remaps.extend(image_remaps)
    guards.extend(image_guards)

    updated_raw, _ = _first_present(fields, "updated_at")
    if updated_raw is None or updated_raw.strip() == "":
        raise ValidationError("missing updated_at", field="updated_at")
    try:
        updated_at_ms = parse_iso_datetime(updated_raw)
    except ValidationError as exc:
        extra = ("BUG-015",) if "/" in updated_raw else ()
        raise ValidationError(exc.message, field=exc.field, bug_guards=extra) from exc

    for column in row.extra_columns:
        remaps.append(f"drop:{column}")

    unique_guards = tuple(dict.fromkeys(guards))
    unique_remaps = tuple(dict.fromkeys(remaps))
    return CanonicalRecord(
        sku=sku,
        title=title,
        price_cents=price_cents,
        currency=currency,
        stock=stock,
        active=active,
        image_url=image_url,
        updated_at_ms=updated_at_ms,
        source_version=row.version,
        row_index=row.index,
        remaps=unique_remaps,
        bug_guards=unique_guards,
    )


_COMPARE_FIELDS = ("title", "price_cents", "currency", "stock", "active", "image_url")


def catalog_diff(before: dict[str, Product], after: dict[str, Product]) -> CompatReport:
    added = tuple(sorted(set(after) - set(before)))
    removed = tuple(sorted(set(before) - set(after)))
    changed: list[CompatChange] = []
    for sku in sorted(set(before) & set(after)):
        old = before[sku]
        new = after[sku]
        for field_name in _COMPARE_FIELDS:
            old_value = getattr(old, field_name)
            new_value = getattr(new, field_name)
            if old_value != new_value:
                breaking = field_name == "currency"
                changed.append(
                    CompatChange(
                        sku=sku,
                        field=field_name,
                        before=old_value,
                        after=new_value,
                        breaking=breaking,
                    )
                )
    return CompatReport(added=added, removed=removed, changed=tuple(changed))
