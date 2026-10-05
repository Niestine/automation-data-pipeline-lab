"""Strict UTF-8 byte checker. Undecodable bytes are rejected, not smuggled through."""

from __future__ import annotations

from .errors import StrictUtf8Error
from .policy import ENCODING, ERRORS


def check_strict_utf8(data: bytes) -> str:
    """Return the Unicode text of a well-formed UTF-8 byte string.

    Ill-formed sequences raise StrictUtf8Error naming the codec role
    ``strict-utf8`` and the first bad byte. A leading U+FEFF is well-formed
    UTF-8; the interchange profile rejects that signature separately.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("check_strict_utf8 expects bytes")
    data = bytes(data)
    found = _first_bad(data)
    if found is not None:
        offset, bad_byte = found
        raise StrictUtf8Error(
            f"strict-utf8 rejected byte 0x{bad_byte:02x} at offset {offset}",
            bad_byte=bad_byte,
            offset=offset,
        )
    try:
        text = data.decode(ENCODING, ERRORS)
    except UnicodeDecodeError as exc:
        offset = exc.start
        bad_byte = data[offset]
        raise StrictUtf8Error(
            f"strict-utf8 rejected byte 0x{bad_byte:02x} at offset {offset}",
            bad_byte=bad_byte,
            offset=offset,
        ) from exc
    if text.encode(ENCODING, ERRORS) != data:
        bad_byte = data[0] if data else 0
        raise StrictUtf8Error(
            f"strict-utf8 rejected byte 0x{bad_byte:02x} at offset 0",
            bad_byte=bad_byte,
            offset=0,
        )
    return text


def _first_bad(data: bytes) -> tuple[int, int] | None:
    """Return ``(offset, byte)`` for the first byte that breaks RFC 3629.

    Second-byte bounds are the shortest-form and surrogate limits: E0 needs
    A0-BF, ED needs 80-9F, F0 needs 90-BF, and F4 needs 80-8F.
    """

    index = 0
    limit = len(data)
    while index < limit:
        lead = data[index]
        if lead <= 0x7F:
            index += 1
            continue
        if lead < 0xC2 or lead > 0xF4:
            return index, lead
        if lead <= 0xDF:
            width = 2
            second_lo, second_hi = 0x80, 0xBF
        elif lead == 0xE0:
            width = 3
            second_lo, second_hi = 0xA0, 0xBF
        elif lead == 0xED:
            width = 3
            second_lo, second_hi = 0x80, 0x9F
        elif lead <= 0xEF:
            width = 3
            second_lo, second_hi = 0x80, 0xBF
        elif lead == 0xF0:
            width = 4
            second_lo, second_hi = 0x90, 0xBF
        elif lead <= 0xF3:
            width = 4
            second_lo, second_hi = 0x80, 0xBF
        else:
            width = 4
            second_lo, second_hi = 0x80, 0x8F
        if index + width > limit:
            return index, lead
        second = data[index + 1]
        if not second_lo <= second <= second_hi:
            return index + 1, second
        for cursor in range(2, width):
            tail = data[index + cursor]
            if not 0x80 <= tail <= 0xBF:
                return index + cursor, tail
        index += width
    return None
