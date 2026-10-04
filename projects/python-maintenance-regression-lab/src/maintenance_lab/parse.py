"""CSV / JSON feed parsers with dialect sniffing and quoted-field handling."""

from __future__ import annotations

from typing import Any, Optional
import csv
import io

from .decode import split_preamble
from .errors import ParseError, SchemaError
from .models import CURRENCIES, FEED_VERSIONS, HEADER_ALIASES, TRAILING_CELLS, RawRow
from .schema import V2_FEED_SCHEMA, V2_ITEM_SCHEMA, check_schema, parse_json_text, require_schema

_DELIMITERS = (",", ";", "\t", "|")


def sniff_delimiter(header_line: str) -> str:
    counts = {delimiter: header_line.count(delimiter) for delimiter in _DELIMITERS}
    best = max(counts, key=lambda key: counts[key])
    if counts[best] == 0:
        raise ParseError("could not sniff a CSV delimiter")
    return best


def _stringify(value: Any) -> tuple[str, str]:
    if value is None:
        return "", "null"
    if type(value) is bool:
        return ("true" if value else "false"), "bool"
    if type(value) is int:
        return str(value), "int"
    if type(value) is str:
        return value, "str"
    raise ParseError(f"unsupported JSON value type {type(value).__name__}")


def _normalize_header(name: str) -> str:
    key = name.strip().lower()
    return HEADER_ALIASES.get(key, key)


def parse_feed(
    text: str,
    *,
    declared_version: Optional[str] = None,
    declared_currency: Optional[str] = None,
    declared_format: Optional[str] = None,
) -> tuple[dict[str, str], list[RawRow]]:
    meta, body, consumed = split_preamble(text)
    version = declared_version or meta.get("version") or "v1"
    if version not in FEED_VERSIONS:
        raise ParseError(f"unknown feed version {version}")
    currency = declared_currency or meta.get("currency")
    if currency is not None and currency not in CURRENCIES:
        raise ParseError(f"unknown currency {currency}")
    fmt = declared_format or meta.get("format")
    stripped = body.lstrip()
    if fmt is None:
        fmt = "json" if stripped[:1] in ("{", "[") else "csv"
    if fmt == "json":
        rows = _parse_json(body, version=version if declared_version or meta.get("version") else "v2", currency=currency)
        if rows:
            version = rows[0].version
    elif fmt in {"csv", "tsv"}:
        delimiter = meta.get("delimiter")
        if fmt == "tsv":
            delimiter = "\t"
        rows = _parse_csv(
            body,
            version=version,
            currency=currency,
            delimiter=delimiter,
            line_offset=consumed,
        )
    else:
        raise ParseError(f"unknown feed format {fmt}")
    meta = dict(meta)
    meta["version"] = version
    meta["format"] = fmt
    if currency:
        meta["currency"] = currency
    return meta, rows


def _parse_json(body: str, *, version: str, currency: Optional[str]) -> list[RawRow]:
    try:
        data = parse_json_text(body)
    except SchemaError as exc:
        raise ParseError(exc.message) from exc
    root_currency = currency
    items: list[Any]
    if isinstance(data, list):
        items = data
        feed_version = version or "v2"
    elif isinstance(data, dict):
        try:
            require_schema(data, V2_FEED_SCHEMA, "feed")
        except SchemaError as exc:
            raise ParseError(exc.message) from exc
        items = data["items"]
        feed_version = str(data.get("version") or version or "v2")
        root_currency = str(data["currency"]) if data.get("currency") else currency
    else:
        raise ParseError("JSON feed must be an object or array")
    if feed_version not in FEED_VERSIONS:
        raise ParseError(f"unknown feed version {feed_version}")
    rows: list[RawRow] = []
    for index, item in enumerate(items, start=1):
        errors = check_schema(item, V2_ITEM_SCHEMA, f"$[{index}]")
        if errors:
            raise ParseError(f"items[{index - 1}] is invalid: {errors[0]}")
        if not isinstance(item, dict):
            raise ParseError(f"items[{index - 1}] must be an object")
        fields: dict[str, str] = {}
        origins: dict[str, str] = {}
        extra: list[str] = []
        for key, value in item.items():
            mapped = _normalize_header(str(key))
            try:
                text, origin = _stringify(value)
            except ParseError as exc:
                raise ParseError(f"items[{index - 1}].{key}: {exc.message}") from exc
            if mapped in fields:
                raise ParseError(f"items[{index - 1}] has duplicate field {mapped}")
            fields[mapped] = text
            origins[mapped] = origin
            if mapped not in HEADER_ALIASES.values() and mapped not in HEADER_ALIASES:
                extra.append(mapped)
        rows.append(
            RawRow(
                index=index,
                line_num=None,
                fields=fields,
                origins=origins,
                format="json",
                version=feed_version,
                decimal_comma=False,
                currency_hint=root_currency,
                extra_columns=tuple(extra),
            )
        )
    return rows


def _parse_csv(
    body: str,
    *,
    version: str,
    currency: Optional[str],
    delimiter: Optional[str],
    line_offset: int,
) -> list[RawRow]:
    if not body.strip():
        raise ParseError("CSV feed is empty")
    first_line = ""
    for line in body.splitlines():
        if line.strip():
            first_line = line
            break
    delim = delimiter or sniff_delimiter(first_line)
    if delim not in _DELIMITERS:
        raise ParseError(f"unsupported delimiter {delim!r}")
    decimal_comma = delim == ";"
    reader = csv.reader(
        io.StringIO(body),
        delimiter=delim,
        quotechar='"',
        doublequote=True,
        skipinitialspace=True,
    )
    try:
        header_row = next(reader)
    except StopIteration as exc:
        raise ParseError("CSV feed is missing a header") from exc
    header = [_normalize_header(name) for name in header_row]
    if not header or not any(header):
        raise ParseError("CSV header is empty")
    lowered = [name.lower() for name in header]
    if len(lowered) != len(set(lowered)):
        raise ParseError("duplicate CSV header")
    extra_header = tuple(
        name for name in header if name not in HEADER_ALIASES.values() and name not in HEADER_ALIASES
    )
    rows: list[RawRow] = []
    for record in reader:
        if not record or all(not cell.strip() for cell in record):
            continue
        fields: dict[str, str] = {}
        origins: dict[str, str] = {}
        for index, name in enumerate(header):
            value = record[index] if index < len(record) else ""
            fields[name] = value
            origins[name] = "str"
        extra = list(extra_header)
        # Extra non-empty cells mean an unquoted delimiter shifted the row
        # (BUG-002). compat.adapt_row rejects it instead of dropping data.
        if any(cell.strip() for cell in record[len(header) :]):
            extra.append(TRAILING_CELLS)
        rows.append(
            RawRow(
                index=len(rows) + 1,
                line_num=reader.line_num + line_offset,
                fields=fields,
                origins=origins,
                format="csv",
                version=version,
                decimal_comma=decimal_comma,
                currency_hint=currency,
                extra_columns=tuple(extra),
            )
        )
    return rows
