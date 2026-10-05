"""Canonical bytes, CRC-32, snapshot header, and length-prefixed log records."""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass

from recovery_lab.errors import StructuralCorruption

MAGIC = b"RL04\n"
LOG_NAME = "ledger.log"
SNAP_NAME = "ledger.snap"
HINT_NAME = "checkpoint.hint"

KIND_TO_CODE = {
    "update": 1,
    "commit": 2,
    "abort": 3,
    "compensation": 4,
    "checkpoint": 5,
}
CODE_TO_KIND = {code: name for name, code in KIND_TO_CODE.items()}

# lsn, prev, tx, kind, schema, undo_next, undone, before_len, after_len, crc
MIN_PAYLOAD = 8 + 8 + 8 + 1 + 4 + 8 + 8 + 4 + 4 + 4


def canonical_json(payload: object) -> bytes:
    """UTF-8 JSON with sorted keys, compact separators, and a trailing newline."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass
class Record:
    lsn: int
    prev_lsn: int
    tx_id: int
    kind: str
    schema_version: int
    undo_next_lsn: int = 0
    undone_lsn: int = 0
    before: dict | None = None
    after: dict | None = None

    @property
    def is_unknown(self) -> bool:
        return self.kind.startswith("unknown")


def encode_record(record: Record) -> bytes:
    before = b"" if record.before is None else canonical_json(record.before)
    after = b"" if record.after is None else canonical_json(record.after)
    kind_code = KIND_TO_CODE.get(record.kind, 0)
    if kind_code == 0:
        raise ValueError(f"cannot encode kind {record.kind}")
    body = struct.pack(
        "<QQQBIQQ",
        record.lsn,
        record.prev_lsn,
        record.tx_id,
        kind_code,
        record.schema_version,
        record.undo_next_lsn,
        record.undone_lsn,
    )
    body += struct.pack("<I", 0xFFFFFFFF if record.before is None else len(before))
    body += before
    body += struct.pack("<I", 0xFFFFFFFF if record.after is None else len(after))
    body += after
    payload = body + struct.pack("<I", crc32(body))
    return struct.pack("<I", len(payload)) + payload


def _decode_body(body: bytes, *, fallback_lsn: int) -> Record:
    fixed = 8 + 8 + 8 + 1 + 4 + 8 + 8
    if len(body) < fixed + 8:
        raise StructuralCorruption(
            "log record body is shorter than the fixed header",
            lsn=fallback_lsn,
            last_durable_lsn=fallback_lsn - 1 if fallback_lsn else 0,
        )
    lsn, prev, tx_id, kind_code, schema, undo_next, undone = struct.unpack_from(
        "<QQQBIQQ", body, 0
    )
    offset = fixed
    before, offset = _take_json(body, offset, fallback_lsn=lsn)
    after, offset = _take_json(body, offset, fallback_lsn=lsn)
    if offset != len(body):
        raise StructuralCorruption(
            f"log record {lsn} has trailing bytes inside a valid CRC",
            lsn=lsn,
            last_durable_lsn=fallback_lsn - 1 if fallback_lsn else 0,
        )
    kind = CODE_TO_KIND.get(kind_code, f"unknown-{kind_code}")
    return Record(
        lsn=lsn,
        prev_lsn=prev,
        tx_id=tx_id,
        kind=kind,
        schema_version=schema,
        undo_next_lsn=undo_next,
        undone_lsn=undone,
        before=before,
        after=after,
    )


def _take_json(body: bytes, offset: int, *, fallback_lsn: int) -> tuple[dict | None, int]:
    if offset + 4 > len(body):
        raise StructuralCorruption(
            "truncated JSON length inside a framed record",
            lsn=fallback_lsn,
            last_durable_lsn=fallback_lsn,
        )
    (length,) = struct.unpack_from("<I", body, offset)
    offset += 4
    if length == 0xFFFFFFFF:
        return None, offset
    if offset + length > len(body):
        raise StructuralCorruption(
            "JSON payload runs past the framed record",
            lsn=fallback_lsn,
            last_durable_lsn=fallback_lsn,
        )
    raw = body[offset : offset + length]
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuralCorruption(
            "checksummed JSON did not decode",
            lsn=fallback_lsn,
            last_durable_lsn=fallback_lsn,
        ) from exc
    if not isinstance(value, dict):
        raise StructuralCorruption(
            "log JSON value is not an object",
            lsn=fallback_lsn,
            last_durable_lsn=fallback_lsn,
        )
    return value, offset + length


def _scan(blob: bytes) -> tuple[list[Record], int]:
    """Return the intact records and the byte length they occupy.

    A short frame at EOF is a torn tail and ends the scan. A frame that fits
    but whose CRC does not match is a hole: ``StructuralCorruption``.
    """
    records: list[Record] = []
    index = 0
    last_lsn = 0
    while index + 4 <= len(blob):
        (length,) = struct.unpack_from("<I", blob, index)
        frame_end = index + 4 + length
        if length < MIN_PAYLOAD or frame_end > len(blob):
            break
        payload = blob[index + 4 : frame_end]
        body, crc_bytes = payload[:-4], payload[-4:]
        (expected,) = struct.unpack("<I", crc_bytes)
        if crc32(body) != expected:
            raise StructuralCorruption(
                f"CRC hole after LSN {last_lsn}",
                lsn=last_lsn + 1,
                last_durable_lsn=last_lsn,
            )
        record = _decode_body(body, fallback_lsn=last_lsn + 1)
        records.append(record)
        last_lsn = record.lsn
        index = frame_end
    return records, index


def parse_log(blob: bytes) -> list[Record]:
    """Parse the intact prefix, dropping a torn tail and raising on a hole."""
    return _scan(blob)[0]


def intact_log_bytes(blob: bytes) -> bytes:
    """Return the byte prefix occupied by complete records, or raise on a hole."""
    return blob[: _scan(blob)[1]]
