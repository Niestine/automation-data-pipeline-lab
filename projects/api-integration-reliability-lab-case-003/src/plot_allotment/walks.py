"""Offset and keyset cost models. These are not the export API."""

from __future__ import annotations

import bisect
from typing import TypeVar

T = TypeVar("T")


def offset_slice(rows: list[T], offset: int, limit: int) -> tuple[list[T], int]:
    """Count every discarded prefix row. That is the offset cost."""
    if offset < 0 or limit < 0:
        raise ValueError("offset and limit must be >= 0")
    walked = 0
    out: list[T] = []
    for index, row in enumerate(rows):
        if walked >= offset + limit:
            break
        walked += 1
        if index >= offset:
            out.append(row)
    return out, walked


def keyset_slice(
    rows: list[T], cursor: T | None, limit: int
) -> tuple[list[T], int]:
    """Seek with bisect, then read one page. The prefix is not walked."""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if cursor is None:
        start = 0
    else:
        start = bisect.bisect_right(rows, cursor)
    page = rows[start : start + limit]
    return page, len(page)
