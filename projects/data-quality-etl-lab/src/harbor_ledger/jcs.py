"""JSON Canonicalization Scheme (RFC 8785) for the sealed anomaly report.

Object members sort by UTF-16 code units, not by Python code-point order.
That difference is visible when a key contains an emoji: U+1F600 sorts before
U+FB33 in UTF-16 and after it in code-point order. Strings are preserved
exactly, so Unicode normalization has to happen before this function runs.
Every float is refused, including an integral one such as 2.0, so a price
cannot drift through binary floating point. Integer counts must be exactly
representable as IEEE-754 doubles. NaN, Infinity, lone surrogates, and duplicate keys abort.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .errors import JCSError

_SAFE_INT = 2**53 - 1


def utf16_key(value: str) -> tuple[int, ...]:
    encoded = value.encode("utf-16-be", "surrogatepass")
    return tuple(encoded[index] << 8 | encoded[index + 1] for index in range(0, len(encoded), 2))


def canonicalize(value: Any) -> str:
    return "".join(_generate(value))


def canonicalize_items(items: list[tuple[str, Any]]) -> str:
    """Canonicalize an object given as pairs so a duplicate key can be rejected."""

    return _object(items)


def canonicalize_json(text: str) -> str:
    """Parse JSON text, then canonicalize. Whitespace and key order do not survive."""

    try:
        parsed = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise JCSError("input is not JSON") from exc
    return canonicalize(parsed)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise JCSError("duplicate object key")
    return dict(pairs)


def assert_ascii_keys(value: Any) -> None:
    """The anomaly report uses ASCII keys. JCS itself still sorts any Unicode key."""

    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or any(ord(character) > 127 for character in key):
                raise JCSError("report object keys must be ASCII")
            assert_ascii_keys(child)
    elif isinstance(value, list):
        for child in value:
            assert_ascii_keys(child)


def _generate(value: Any) -> list[str]:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["true" if value else "false"]
    if isinstance(value, int):
        if abs(value) > _SAFE_INT:
            raise JCSError("integer is not an exactly representable IEEE-754 integer")
        return [str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JCSError("NaN and Infinity are not permitted")
        raise JCSError("floats must be JSON strings in this profile")
    if isinstance(value, str):
        return [_string(value)]
    if isinstance(value, list):
        parts = ["["]
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            parts.extend(_generate(item))
        parts.append("]")
        return parts
    if isinstance(value, dict):
        pairs = list(value.items())
        return [_object(pairs)]
    raise JCSError(f"unsupported value type {type(value).__name__}")


def _object(items: list[tuple[str, Any]]) -> str:
    keys = []
    for key, _value in items:
        if not isinstance(key, str):
            raise JCSError("object keys must be strings")
        _reject_lone_surrogates(key)
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise JCSError("duplicate object key")
    ordered = sorted(items, key=lambda pair: utf16_key(pair[0]))
    parts = ["{"]
    for index, (key, value) in enumerate(ordered):
        if index:
            parts.append(",")
        parts.append(_string(key))
        parts.append(":")
        parts.extend(_generate(value))
    parts.append("}")
    return "".join(parts)


def _string(value: str) -> str:
    _reject_lone_surrogates(value)
    pieces = ['"']
    for character in value:
        code = ord(character)
        if character == '"':
            pieces.append('\\"')
        elif character == "\\":
            pieces.append("\\\\")
        elif character == "\b":
            pieces.append("\\b")
        elif character == "\t":
            pieces.append("\\t")
        elif character == "\n":
            pieces.append("\\n")
        elif character == "\f":
            pieces.append("\\f")
        elif character == "\r":
            pieces.append("\\r")
        elif code < 0x20:
            pieces.append(f"\\u{code:04x}")
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _reject_lone_surrogates(value: str) -> None:
    for character in value:
        if 0xD800 <= ord(character) <= 0xDFFF:
            raise JCSError("lone surrogate")
