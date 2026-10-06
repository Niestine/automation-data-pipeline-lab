"""Stage 4. Typed cells. A bad cell is recorded and the file continues.

Reader defaults are not applied here. An encoded empty string stays empty.
Stage 5 fills a default only when the writer omitted the column.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Optional

from .model import Column


_INT32_MIN = -2147483648
_INT32_MAX = 2147483647

_DATE_TOKENS = (
    ("yyyy", r"(\d{4})", "year"),
    ("MM", r"(\d{2})", "month"),
    ("dd", r"(\d{2})", "day"),
    ("HH", r"(\d{2})", "hour"),
    ("mm", r"(\d{2})", "minute"),
    ("ss", r"(\d{2})", "second"),
    ("M", r"(\d{1,2})", "month"),
    ("d", r"(\d{1,2})", "day"),
    ("H", r"(\d{1,2})", "hour"),
    ("m", r"(\d{1,2})", "minute"),
    ("s", r"(\d{1,2})", "second"),
)


def fold_whitespace(value: str) -> str:
    chars = []
    for char in value:
        if char in "\r\n\t":
            chars.append(" ")
        else:
            chars.append(char)
    folded = "".join(chars).strip()
    return re.sub(r" {2,}", " ", folded)


def _parse_decimal(text: str, decimal_char: str, group_char: str) -> Optional[tuple]:
    raw = text
    if not raw:
        return None
    percent = False
    if raw.endswith("%"):
        percent = True
        raw = raw[:-1]
        if not raw:
            return None
    sign = 1
    if raw[0] in "+-":
        if raw[0] == "-":
            sign = -1
        raw = raw[1:]
    exponent = 0
    lower = raw.lower()
    if "e" in lower:
        exp_at = lower.index("e")
        exp_text = raw[exp_at + 1 :]
        raw = raw[:exp_at]
        if not exp_text or not raw:
            return None
        exp_sign = 1
        if exp_text[0] in "+-":
            if exp_text[0] == "-":
                exp_sign = -1
            exp_text = exp_text[1:]
        if not exp_text.isdigit():
            return None
        exponent = exp_sign * int(exp_text)
    if decimal_char and decimal_char in raw:
        whole, frac = raw.split(decimal_char, 1)
        if decimal_char in frac:
            return None
    else:
        whole, frac = raw, None
    if group_char:
        if group_char in (frac or ""):
            return None
        if group_char in whole:
            parts = whole.split(group_char)
            if len(parts) < 2 or not parts[0].isdigit():
                return None
            if any(len(part) != 3 or not part.isdigit() for part in parts[1:]):
                return None
            whole = "".join(parts)
    if not whole.isdigit():
        return None
    if frac is not None:
        if not frac.isdigit():
            return None
        scale = len(frac)
        digits = whole + "." + frac
    else:
        scale = 0
        digits = whole
    try:
        value = Decimal(digits) * sign
    except InvalidOperation:
        return None
    if exponent:
        value = value.scaleb(exponent)
        scale = max(0, scale - exponent) if exponent > 0 else scale + abs(exponent)
    if percent:
        value = value / Decimal(100)
        scale += 2
    return value, scale


def _format_decimal(value: Decimal, scale: int) -> str:
    if scale < 0:
        scale = 0
    quant = Decimal(1).scaleb(-scale)
    return format(value.quantize(quant), "f")


def _compile_pattern(pattern: str):
    regex = []
    names = []
    index = 0
    while index < len(pattern):
        matched = False
        for token, group, name in _DATE_TOKENS:
            if pattern.startswith(token, index):
                regex.append(group)
                names.append(name)
                index += len(token)
                matched = True
                break
        if not matched:
            regex.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(regex) + "$"), names


def _parse_date(text: str, pattern: str, with_time: bool) -> Optional[str]:
    if not pattern:
        return None
    compiled, names = _compile_pattern(pattern)
    found = compiled.match(text)
    if not found:
        return None
    parts = {"year": 1, "month": 1, "day": 1, "hour": 0, "minute": 0, "second": 0}
    for name, value in zip(names, found.groups()):
        parts[name] = int(value)
    try:
        if with_time:
            stamp = datetime(
                parts["year"],
                parts["month"],
                parts["day"],
                parts["hour"],
                parts["minute"],
                parts["second"],
            )
            return stamp.strftime("%Y-%m-%dT%H:%M:%S")
        return date(parts["year"], parts["month"], parts["day"]).isoformat()
    except ValueError:
        return None


def _parse_boolean(text: str, fmt: Optional[str]) -> Optional[bool]:
    token = text.casefold()
    if fmt and "/" in fmt:
        left, right = fmt.split("/", 1)
        if token == left.casefold():
            return True
        if token == right.casefold():
            return False
        return None
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


def parse_cell(raw: str, col: Column) -> dict:
    """Parse one present field. `value` is JSON-ready."""
    prepared = raw
    if col.datatype != "string":
        prepared = fold_whitespace(raw)
    if prepared in col.null_tokens or raw in col.null_tokens:
        value = None
        errors = ["required_null"] if col.required else []
        return {"value": value, "errors": errors}
    if col.datatype == "string":
        # W3C maxLength: a violation is a cell error and the string stays.
        if col.max_length is not None and len(prepared) > col.max_length:
            return {"value": prepared, "errors": ["length_error"]}
        return {"value": prepared, "errors": []}
    if col.datatype in {"integer", "long"}:
        # Integers are ASCII digits with an optional sign. Exponents, fraction
        # digits, and group characters are type errors.
        if not re.fullmatch(r"[+-]?[0-9]+", prepared):
            return {"value": raw, "errors": ["type_error"]}
        number = int(prepared)
        if col.datatype == "integer" and not _INT32_MIN <= number <= _INT32_MAX:
            return {"value": raw, "errors": ["type_error"]}
        return {"value": number, "errors": []}
    if col.datatype == "decimal":
        parsed = _parse_decimal(prepared, col.decimal_char, col.group_char)
        if parsed is None:
            return {"value": raw, "errors": ["type_error"]}
        return {"value": _format_decimal(parsed[0], parsed[1]), "errors": []}
    if col.datatype == "double":
        parsed = _parse_decimal(prepared, col.decimal_char, col.group_char)
        if parsed is None:
            return {"value": raw, "errors": ["type_error"]}
        as_float = float(parsed[0])
        if not math.isfinite(as_float):
            return {"value": raw, "errors": ["type_error"]}
        if as_float.is_integer():
            return {"value": int(as_float), "errors": []}
        return {"value": as_float, "errors": []}
    if col.datatype == "boolean":
        parsed_bool = _parse_boolean(prepared, col.format)
        if parsed_bool is None:
            return {"value": raw, "errors": ["type_error"]}
        return {"value": parsed_bool, "errors": []}
    if col.datatype == "date":
        parsed_date = _parse_date(prepared, col.format or "", False)
        if parsed_date is None:
            return {"value": raw, "errors": ["type_error"]}
        return {"value": parsed_date, "errors": []}
    if col.datatype == "datetime":
        parsed_stamp = _parse_date(prepared, col.format or "", True)
        if parsed_stamp is None:
            return {"value": raw, "errors": ["type_error"]}
        return {"value": parsed_stamp, "errors": []}
    return {"value": raw, "errors": ["type_error"]}


def promote(value: Any, writer_type: str, reader_type: str) -> dict:
    """Avro-style widening. Narrowing is refused by the caller before this."""
    if writer_type == reader_type or value is None or isinstance(value, str):
        return {"value": value, "precision_loss": False}
    if reader_type == "long" and writer_type == "integer":
        return {"value": int(value), "precision_loss": False}
    if reader_type == "decimal" and writer_type == "integer":
        return {"value": str(int(value)), "precision_loss": False}
    if reader_type == "double" and writer_type in {"integer", "long"}:
        number = int(value)
        as_float = float(number)
        lost = int(as_float) != number
        rendered = int(as_float) if as_float.is_integer() else as_float
        return {"value": rendered, "precision_loss": lost, "source_integer": str(number)}
    return {"value": value, "precision_loss": False}
