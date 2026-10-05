"""Foreign ingest: strict UTF-8, then logical lines. The locale codec is not used."""

from __future__ import annotations

from pathlib import Path

from .errors import LeadingBomError, StrictUtf8Error
from .logsetup import log_boundary
from .policy import BOM
from .utf8strict import check_strict_utf8


def logical_lines(text: str) -> list[str]:
    """Split on CRLF, then LF, then CR. U+0085, U+2028, and U+2029 stay in the line."""

    lines: list[str] = []
    start = 0
    index = 0
    limit = len(text)
    while index < limit:
        if text.startswith("\r\n", index):
            lines.append(text[start:index])
            index += 2
            start = index
            continue
        if text[index] in ("\n", "\r"):
            lines.append(text[start:index])
            index += 1
            start = index
            continue
        index += 1
    lines.append(text[start:])
    return lines


def read_foreign_text(path) -> str:
    """Read ``path`` as bytes and return logical text joined by ``\\n``."""

    data = Path(path).read_bytes()
    try:
        if data.startswith(BOM):
            raise LeadingBomError()
        text = check_strict_utf8(data)
    except StrictUtf8Error as exc:
        log_boundary("foreign-ingest", exc.codec, bad_byte=exc.bad_byte)
        raise
    return "\n".join(logical_lines(text))
