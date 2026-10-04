"""Shared SLIP scanner. Legacy and hardened supply a frozen policy."""

from __future__ import annotations

import time
from dataclasses import dataclass

BRANCHES = (
    "empty",
    "nul",
    "carriage",
    "unclosed_quote",
    "bare_quote",
    "field_count",
    "hash_strip",
    "hang_reject",
    "header",
    "records",
)

_counts = {name: 0 for name in BRANCHES}


@dataclass(frozen=True)
class Policy:
    name: str
    doubled_quotes: bool
    drop_trailing_empty: bool
    nul_mode: str
    cr_mode: str
    hang_seconds: float
    strip_hash_lines: bool
    reject_hang_token: bool


@dataclass
class Rec:
    values: list[str]
    raw_fields: list[str]
    raw: str
    trailing_sep: bool


def reset_branches() -> None:
    for name in _counts:
        _counts[name] = 0


def branch_counts() -> dict[str, int]:
    return dict(_counts)


def untouched_branches() -> list[str]:
    return [name for name in BRANCHES if _counts[name] == 0]


def _hit(name: str, policy: Policy) -> None:
    if policy.name == "hardened":
        _counts[name] = _counts.get(name, 0) + 1


def scan(blob: bytes, policy: Policy) -> tuple:
    """Parse one blob under a frozen policy."""

    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError("blob must be bytes")
    raw = bytes(blob)
    if policy.hang_seconds and b"__HANG__" in raw:
        time.sleep(policy.hang_seconds)
    text = raw.decode("latin-1")
    if text == "":
        _hit("empty", policy)
        return ("E_EMPTY_DOCUMENT",)
    if "\x00" in text:
        _hit("nul", policy)
        if policy.nul_mode == "crash":
            raise RuntimeError("nul-byte")
        return ("E_NUL",)
    if "\r" in text:
        _hit("carriage", policy)
        if policy.cr_mode == "crash":
            raise RuntimeError("carriage-return")
        return ("E_CARRIAGE",)
    if policy.reject_hang_token and "__HANG__" in text:
        _hit("hang_reject", policy)
        return ("E_HANG_TOKEN",)
    recs, err = _parse_document(text, policy)
    if err is not None:
        if err[0] == "E_UNCLOSED_QUOTE":
            _hit("unclosed_quote", policy)
        elif err[0] == "E_BARE_QUOTE":
            _hit("bare_quote", policy)
        return err
    if policy.strip_hash_lines:
        _hit("hash_strip", policy)
        recs = [rec for rec in recs if not rec.raw.startswith("#")]
    if not recs:
        if policy.strip_hash_lines:
            _hit("records", policy)
            return ("records", None, ())
        _hit("empty", policy)
        return ("E_EMPTY_DOCUMENT",)
    header = None
    data = recs
    if recs[0].values and recs[0].values[0] == "@slip":
        _hit("header", policy)
        header = tuple(recs[0].values[1:])
        data = recs[1:]
        if len(header) == 0:
            _hit("field_count", policy)
            return ("E_FIELD_COUNT",)
        for rec in data:
            if len(rec.values) != len(header):
                _hit("field_count", policy)
                return ("E_FIELD_COUNT",)
    rows = tuple(tuple(rec.values) for rec in data)
    _hit("records", policy)
    return ("records", header, rows)


def raw_field_table(blob: bytes, policy: Policy) -> list[list[str]] | None:
    """Raw field surfaces for a syntactically accepted blob."""

    if not isinstance(blob, (bytes, bytearray)):
        return None
    text = bytes(blob).decode("latin-1")
    if text == "" or "\x00" in text or "\r" in text:
        return None
    if policy.reject_hang_token and "__HANG__" in text:
        return None
    recs, err = _parse_document(text, policy)
    if err is not None or not recs:
        return None
    return [list(rec.raw_fields) for rec in recs]


def _parse_document(text: str, policy: Policy) -> tuple[list[Rec] | None, tuple | None]:
    recs: list[Rec] = []
    index = 0
    limit = len(text)
    while index < limit:
        rec, index, err = _parse_record(text, index, policy)
        if err is not None:
            return None, err
        assert rec is not None
        recs.append(rec)
        # A record stops at a newline or at the end of the text. Step
        # past it; a final newline does not open another record.
        index += 1
    return recs, None


def _parse_record(text: str, index: int, policy: Policy) -> tuple[Rec | None, int, tuple | None]:
    start = index
    limit = len(text)
    values: list[str] = []
    raws: list[str] = []
    buf: list[str] = []
    field_start = index
    in_quote = False
    trailing_sep = False
    while index < limit and text[index] != "\n":
        char = text[index]
        if in_quote:
            if char == '"':
                if policy.doubled_quotes and index + 1 < limit and text[index + 1] == '"':
                    buf.append('"')
                    index += 2
                    trailing_sep = False
                    continue
                in_quote = False
                index += 1
                trailing_sep = False
                continue
            buf.append(char)
            index += 1
            trailing_sep = False
            continue
        if char == '"':
            if buf:
                return None, index, ("E_BARE_QUOTE",)
            in_quote = True
            index += 1
            trailing_sep = False
            continue
        if char == "|":
            values.append("".join(buf))
            raws.append(text[field_start:index])
            buf = []
            index += 1
            field_start = index
            trailing_sep = True
            continue
        buf.append(char)
        index += 1
        trailing_sep = False
    if in_quote:
        return None, index, ("E_UNCLOSED_QUOTE",)
    values.append("".join(buf))
    raws.append(text[field_start:index])
    if policy.drop_trailing_empty and trailing_sep and len(values) >= 2 and values[-1] == "":
        values.pop()
        raws.pop()
    rec = Rec(values, raws, text[start:index], trailing_sep)
    return rec, index, None
