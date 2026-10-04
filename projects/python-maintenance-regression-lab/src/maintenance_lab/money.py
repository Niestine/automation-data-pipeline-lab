"""Integer minor-unit money parsing.

``float("19.99") * 100`` is 1998.999... on IEEE-754 (BUG-003). Prices are
parsed with ``Decimal`` and stored as integer cents (USD/EUR) or yen (JPY).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from .errors import ValidationError
from .models import CURRENCIES, CURRENCY_DECIMALS

_SYMBOLS = {"USD": "$", "EUR": "€", "JPY": "¥"}
_MAX = Decimal("100000000")


def parse_price(raw: str, currency: str, *, decimal_comma: bool = False) -> int:
    if currency not in CURRENCIES:
        raise ValidationError(f"unknown currency {currency}", field="price")
    text = raw.strip().replace(" ", "")
    if not text:
        raise ValidationError("empty price", field="price")
    symbol = _SYMBOLS[currency]
    if text.startswith(symbol):
        text = text[len(symbol) :].strip()
    elif text.endswith(currency):
        text = text[: -len(currency)].strip()
    text = _normalize_separators(text, currency, decimal_comma=decimal_comma)
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationError(f"invalid price {raw!r}", field="price") from exc
    if amount < 0:
        raise ValidationError("negative price", field="price")
    if amount > _MAX:
        raise ValidationError("price too large", field="price")
    decimals = CURRENCY_DECIMALS[currency]
    quant = Decimal("1").scaleb(-decimals)
    quantized = amount.quantize(quant, rounding=ROUND_HALF_EVEN)
    if quantized != amount:
        raise ValidationError(
            f"{currency} price has too many decimal places",
            field="price",
        )
    minor = quantized * (Decimal(10) ** decimals)
    return int(minor)


def _normalize_separators(text: str, currency: str, *, decimal_comma: bool) -> str:
    if decimal_comma:
        if "." in text and "," in text:
            if text.rfind(",") > text.rfind("."):
                return text.replace(".", "").replace(",", ".")
            raise ValidationError("ambiguous decimal comma price", field="price")
        if "," in text:
            return text.replace(",", ".")
        return text
    if "," in text and "." in text:
        if text.rfind(".") > text.rfind(","):
            return text.replace(",", "")
        raise ValidationError("ambiguous thousands separator", field="price")
    if "," in text:
        parts = text.split(",")
        if currency == "JPY" and all(part.isdigit() for part in parts) and parts[0]:
            return "".join(parts)
        raise ValidationError(
            "comma in price requires an EU delimiter or JPY thousands grouping",
            field="price",
        )
    return text


def format_minor(price_cents: int, currency: str) -> str:
    decimals = CURRENCY_DECIMALS[currency]
    if decimals == 0:
        return str(int(price_cents))
    quant = Decimal(10) ** decimals
    amount = (Decimal(int(price_cents)) / quant).quantize(Decimal("1").scaleb(-decimals))
    return format(amount, "f")
