"""Remove Content-Encoding before a checksum is taken.

gzip and deflate use the standard library. ``compress`` is the UNIX compress
LZW stream (magic 1F 9D, block mode off) with a matching writer used by the
fixture. Bytes are never hashed while still compressed.
"""

from __future__ import annotations

import gzip
import zlib

from incremental_crawl_lab.errors import UnsupportedEncoding


def gunzip(body: bytes) -> bytes:
    return gzip.decompress(body)


def inflate(body: bytes) -> bytes:
    try:
        return zlib.decompress(body)
    except zlib.error:
        return zlib.decompress(body, -zlib.MAX_WBITS)


def compress_lzw(data: bytes, max_bits: int = 12) -> bytes:
    """LZW writer. Block mode is off. Codes are packed least-significant bit first."""
    if not 9 <= max_bits <= 16:
        raise ValueError("max_bits must be from 9 to 16")
    mapping: dict[bytes, int] = {bytes([index]): index for index in range(256)}
    next_code = 256
    width = 9
    codes: list[tuple[int, int]] = []
    window = b""
    limit = 1 << max_bits

    def emit(code: int, bits: int) -> None:
        codes.append((code, bits))

    for raw in data:
        nxt = window + bytes([raw])
        if nxt in mapping:
            window = nxt
            continue
        emit(mapping[window], width)
        if next_code < limit:
            mapping[nxt] = next_code
            next_code += 1
            if next_code > (1 << width) and width < max_bits:
                width += 1
        window = bytes([raw])
    if window:
        emit(mapping[window], width)
    return bytes([0x1F, 0x9D, max_bits]) + _pack(codes)


def decompress_lzw(data: bytes) -> bytes:
    if len(data) < 3 or data[0] != 0x1F or data[1] != 0x9D:
        raise UnsupportedEncoding("compress header")
    max_bits = data[2] & 0x1F
    if data[2] & 0x80:
        raise UnsupportedEncoding("compress block mode")
    if not 9 <= max_bits <= 16:
        raise UnsupportedEncoding("compress max bits")
    stream = _BitReader(data[3:])
    mapping: dict[int, bytes] = {index: bytes([index]) for index in range(256)}
    next_code = 256
    width = 9
    limit = 1 << max_bits
    output = bytearray()
    prev: bytes | None = None
    while True:
        # The encoder defines a code on emit and may widen before the next emit.
        # The decoder defines that same code one step later, so the width check
        # looks one code ahead.
        if prev is not None and next_code + 1 > (1 << width) and width < max_bits:
            width += 1
        code = stream.read(width)
        if code is None:
            break
        if code < next_code and code in mapping:
            entry = mapping[code]
        elif code == next_code and prev is not None:
            entry = prev + prev[:1]
        else:
            raise UnsupportedEncoding("compress code")
        output.extend(entry)
        if prev is not None and next_code < limit:
            mapping[next_code] = prev + entry[:1]
            next_code += 1
        prev = entry
    return bytes(output)


class _BitReader:
    def __init__(self, blob: bytes) -> None:
        self.blob = blob
        self.acc = 0
        self.bits = 0
        self.index = 0

    def read(self, width: int) -> int | None:
        while self.bits < width:
            if self.index >= len(self.blob):
                return None
            self.acc |= self.blob[self.index] << self.bits
            self.index += 1
            self.bits += 8
        value = self.acc & ((1 << width) - 1)
        self.acc >>= width
        self.bits -= width
        return value


def _pack(codes: list[tuple[int, int]]) -> bytes:
    acc = 0
    bits = 0
    out = bytearray()
    for code, width in codes:
        acc |= code << bits
        bits += width
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits:
        out.append(acc & 0xFF)
    return bytes(out)


def unwrap_encoding(body: bytes, encoding: str | None) -> bytes:
    if encoding is None or encoding.strip() == "" or encoding.strip().lower() == "identity":
        return body
    parts = [part.strip().lower() for part in encoding.split(",") if part.strip()]
    for part in reversed(parts):
        if part == "identity":
            continue
        if part == "gzip":
            body = gunzip(body)
        elif part == "deflate":
            body = inflate(body)
        elif part == "compress":
            body = decompress_lzw(body)
        else:
            raise UnsupportedEncoding(part)
    return body
