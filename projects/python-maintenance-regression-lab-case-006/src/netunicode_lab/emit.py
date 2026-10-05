"""Emit a Net-Unicode CSV. Newline policy and the UTF-8 codec come from policy."""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

from .errors import LabError, LeadingBomError, ProfileError
from .files import atomic_write_bytes, reject_casefold_collision
from .logsetup import log_boundary
from .policy import (
    BOM,
    CSV_DIALECT,
    ENCODING,
    ERRORS,
    OPEN_NEWLINE,
    encode_strict,
    nfc,
    reject_c1,
    reject_line_break,
    reject_unassigned,
)
from .utf8strict import check_strict_utf8


def prepare_field(field: str) -> str:
    if not isinstance(field, str):
        raise TypeError("CSV field must be str")
    try:
        reject_c1(field)
        encode_strict(field)
        normalized = nfc(field)
        reject_unassigned(normalized)
        reject_line_break(normalized)
    except ProfileError as exc:
        log_boundary(
            "interchange-emit",
            exc.codec,
            bad_byte=exc.bad_byte,
            unidata_version=exc.unidata_version,
        )
        raise
    return normalized


def prepare_rows(rows) -> list[list[str]]:
    if isinstance(rows, (str, bytes)):
        raise TypeError("rows must be a sequence of rows")
    prepared: list[list[str]] = []
    for row in rows:
        if isinstance(row, (str, bytes)):
            raise TypeError("each row must be a sequence of str fields")
        prepared.append([prepare_field(field) for field in row])
    return prepared


def assert_interchange_bytes(data: bytes) -> None:
    """Raise unless ``data`` is strict UTF-8, CRLF-only, BOM-free, and free of C1."""

    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("assert_interchange_bytes expects bytes")
    payload = bytes(data)
    try:
        _require_profile(payload)
    except LabError as exc:
        log_boundary(
            "interchange-emit",
            getattr(exc, "codec", "strict-utf8"),
            bad_byte=getattr(exc, "bad_byte", None),
            unidata_version=getattr(exc, "unidata_version", None),
        )
        raise


def emit_interchange_csv(path, rows) -> None:
    """Write ``rows`` as excel-dialect CSV using the policy encoding and newline.

    The csv writer runs against a text file opened with ``encoding`` and
    ``newline`` from :mod:`netunicode_lab.policy`. The handle is closed, the
    bytes are checked, and only then does :func:`atomic_write_bytes` publish.
    """

    destination = Path(path)
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(str(parent))
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(str(destination))
    reject_casefold_collision(destination)
    prepared = prepare_rows(rows)
    descriptor, temp_name = tempfile.mkstemp(prefix=".part-", suffix=".tmp", dir=parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        with temporary.open("w", encoding=ENCODING, newline=OPEN_NEWLINE, errors=ERRORS) as handle:
            csv.writer(handle, dialect=CSV_DIALECT).writerows(prepared)
        payload = temporary.read_bytes()
        assert_interchange_bytes(payload)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink()
    atomic_write_bytes(destination, payload)


def _require_profile(data: bytes) -> None:
    if data.startswith(BOM):
        raise LeadingBomError()
    if b"\r\r\n" in data:
        raise ProfileError(
            "utf-8 profile rejected CR CR LF byte 0x0d",
            codec=ENCODING,
            bad_byte=0x0D,
        )
    index = 0
    limit = len(data)
    while index < limit:
        byte = data[index]
        if byte == 0x0D:
            if index + 1 >= limit or data[index + 1] != 0x0A:
                raise ProfileError(
                    "utf-8 profile rejected bare CR byte 0x0d",
                    codec=ENCODING,
                    bad_byte=0x0D,
                )
            index += 2
            continue
        if byte == 0x0A:
            raise ProfileError(
                "utf-8 profile rejected bare LF byte 0x0a",
                codec=ENCODING,
                bad_byte=0x0A,
            )
        index += 1
    text = check_strict_utf8(data)
    for char in text:
        code = ord(char)
        if 0x80 <= code <= 0x9F:
            raise ProfileError(
                f"utf-8 profile rejected C1 control U+{code:04X}",
                codec=ENCODING,
            )
