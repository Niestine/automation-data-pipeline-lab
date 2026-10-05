"""Unfixed patterns kept for regression contrast. Ingest and emit do not call this module."""

from __future__ import annotations

import os
from pathlib import Path


def read_cp1252(path) -> str:
    """Locale-style text open that surfaces the charmap signature for byte 0x81."""

    with Path(path).open(encoding="cp1252") as handle:
        return handle.read()


def split_on_lf_only(text: str) -> list[str]:
    """The newline bug: a CRLF line keeps a trailing CR, so a LF regex misses."""

    return text.split("\n")


def double_translate_newlines(data: bytes) -> bytes:
    """Apply a second LF-to-CRLF translation to a buffer that already has CRLF."""

    return data.decode("utf-8").replace("\n", "\r\n").encode("utf-8")


def replace_while_open(source, destination, payload: bytes) -> None:
    """Unfixed replace: the source handle is still open inside the with block."""

    source_path = Path(source)
    with source_path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.replace(source_path, destination)


def naive_forbidden_scalar(data: bytes) -> str:
    """The two RFC 3629 illustrations a naive decoder accepts, plus overlong slash."""

    if data == b"\xc0\x80":
        return "\x00"
    if data == b"\xc0\xaf":
        return "/"
    if data == bytes.fromhex("eda18cedbeb4"):
        return "\U000233b4"
    raise ValueError("unknown demonstration vector")
