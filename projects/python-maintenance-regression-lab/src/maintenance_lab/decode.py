"""Defensive feed decoding with encoding sniff and BOM handling."""

from __future__ import annotations

from typing import Optional
import codecs

from .errors import DecodeError
from .models import DecodeResult, PREAMBLE_KEYS


def looks_japanese(text: str) -> bool:
    for char in text:
        code = ord(char)
        if 0x3040 <= code <= 0x30FF or 0x4E00 <= code <= 0x9FFF or 0xFF66 <= code <= 0xFF9D:
            return True
    return False


def parse_preamble_line(line: str) -> dict[str, str]:
    body = line.strip().lstrip("#").strip()
    parsed: dict[str, str] = {}
    for part in body.split():
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key in PREAMBLE_KEYS and value:
            parsed[key] = value
    return parsed


def peek_preamble_hints(data: bytes) -> dict[str, str]:
    head = data.split(b"\n", 1)[0].rstrip(b"\r")
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    try:
        line = head.decode("ascii")
    except UnicodeDecodeError:
        line = head.decode("latin-1")
    if not line.strip().startswith("#"):
        return {}
    return parse_preamble_line(line)


def split_preamble(text: str) -> tuple[dict[str, str], str, int]:
    meta: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    consumed = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            meta.update(parse_preamble_line(stripped))
            consumed += 1
        elif stripped == "":
            consumed += 1
        else:
            break
    body = "".join(lines[consumed:])
    return meta, body, consumed


def _declared_guards(declared: str) -> tuple[str, ...]:
    name = codecs.lookup(declared).name
    if name in {"cp932", "shift_jis", "shift_jis_2004", "shift_jisx0213", "euc_jp"}:
        return ("BUG-001",)
    if name in {"latin-1", "iso8859-1", "cp1252"}:
        return ("BUG-016",)
    return ()


def decode_feed(data: bytes, *, declared_encoding: Optional[str] = None) -> DecodeResult:
    if not data:
        raise DecodeError("empty feed")
    hints = peek_preamble_hints(data)
    declared = (declared_encoding or hints.get("encoding") or "").strip() or None
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DecodeError("utf-8 BOM payload is not valid UTF-8") from exc
        return DecodeResult(text=text, encoding="utf-8-sig", bug_guards=())
    if declared:
        try:
            text = data.decode(declared)
        except LookupError as exc:
            raise DecodeError(f"unknown encoding {declared}") from exc
        except UnicodeDecodeError as exc:
            raise DecodeError(f"declared encoding {declared} failed") from exc
        return DecodeResult(text=text, encoding=declared, bug_guards=_declared_guards(declared))
    try:
        text = data.decode("utf-8")
        return DecodeResult(text=text, encoding="utf-8", bug_guards=())
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp932")
    except UnicodeDecodeError:
        text = None
    if text is not None and looks_japanese(text):
        return DecodeResult(text=text, encoding="cp932", bug_guards=("BUG-001",))
    latin = data.decode("latin-1")
    return DecodeResult(text=latin, encoding="latin-1", bug_guards=("BUG-016",))
