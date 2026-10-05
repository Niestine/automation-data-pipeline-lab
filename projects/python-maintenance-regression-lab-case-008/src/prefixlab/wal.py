"""Write-ahead log: monotonic LSNs, a force watermark, and torn-tail framing."""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass

logger = logging.getLogger("prefixlab.wal")

KINDS = ("begin", "update", "clr", "savepoint", "commit", "abort")


@dataclass(frozen=True)
class LogRecord:
    lsn: int
    prev_lsn: int
    txn: str
    kind: str
    page: str = ""
    before: bytes = b""
    after: bytes = b""
    undo_nxt_lsn: int = 0
    compensates_lsn: int = 0

    def to_json(self) -> dict:
        return {
            "lsn": self.lsn,
            "prev_lsn": self.prev_lsn,
            "txn": self.txn,
            "kind": self.kind,
            "page": self.page,
            "before": self.before.hex(),
            "after": self.after.hex(),
            "undo_nxt_lsn": self.undo_nxt_lsn,
            "compensates_lsn": self.compensates_lsn,
        }

    @classmethod
    def from_json(cls, doc: dict) -> "LogRecord":
        if not isinstance(doc, dict):
            raise ValueError("log record must be an object")
        kind = doc.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown log record kind: {kind!r}")
        try:
            return cls(
                lsn=int(doc["lsn"]),
                prev_lsn=int(doc["prev_lsn"]),
                txn=str(doc["txn"]),
                kind=kind,
                page=str(doc.get("page") or ""),
                before=bytes.fromhex(doc.get("before") or ""),
                after=bytes.fromhex(doc.get("after") or ""),
                undo_nxt_lsn=int(doc.get("undo_nxt_lsn") or 0),
                compensates_lsn=int(doc.get("compensates_lsn") or 0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed log record: {exc}") from exc


def format_record(record: LogRecord) -> str:
    undo = "-" if not record.undo_nxt_lsn else str(record.undo_nxt_lsn)
    page = record.page or "-"
    return (
        f"lsn={record.lsn} txn={record.txn} kind={record.kind} "
        f"page={page} undo_nxt_lsn={undo}"
    )


def encode_records(records: list[LogRecord]) -> bytes:
    chunks = []
    for record in records:
        payload = json.dumps(record.to_json(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        chunks.append(struct.pack(">I", len(payload)))
        chunks.append(payload)
    return b"".join(chunks)


def stable_prefix(raw: bytes) -> list[LogRecord]:
    """Parse length-prefixed records and drop a partial trailing frame."""

    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("stable log bytes must be a bytes object")
    records: list[LogRecord] = []
    index = 0
    view = bytes(raw)
    while index + 4 <= len(view):
        (length,) = struct.unpack(">I", view[index : index + 4])
        index += 4
        if length < 2 or index + length > len(view):
            break
        payload = view[index : index + length]
        index += length
        records.append(LogRecord.from_json(json.loads(payload.decode("utf-8"))))
    return records


class WriteAheadLog:
    """Volatile records plus a force watermark. A crash keeps ``lsn <= watermark``."""

    def __init__(self) -> None:
        self.records: list[LogRecord] = []
        self.stable_upto = 0
        self.next_lsn = 1

    def get(self, lsn: int) -> LogRecord:
        for record in self.records:
            if record.lsn == lsn:
                return record
        raise KeyError(lsn)

    def append(self, **fields) -> LogRecord:
        kind = fields.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown log record kind: {kind!r}")
        record = LogRecord(
            lsn=self.next_lsn,
            prev_lsn=int(fields.get("prev_lsn") or 0),
            txn=str(fields.get("txn") or ""),
            kind=kind,
            page=str(fields.get("page") or ""),
            before=bytes(fields.get("before") or b""),
            after=bytes(fields.get("after") or b""),
            undo_nxt_lsn=int(fields.get("undo_nxt_lsn") or 0),
            compensates_lsn=int(fields.get("compensates_lsn") or 0),
        )
        self.next_lsn += 1
        self.records.append(record)
        logger.info(format_record(record))
        return record

    def force(self, upto: int) -> None:
        if upto > self.stable_upto:
            self.stable_upto = upto

    def stable_records(self) -> list[LogRecord]:
        return [record for record in self.records if record.lsn <= self.stable_upto]

    def volatile_tail(self) -> list[LogRecord]:
        return [record for record in self.records if record.lsn > self.stable_upto]

    def crash_drop_tail(self) -> list[LogRecord]:
        """Drop the unforced tail. Returns the records that were discarded."""

        dropped = self.volatile_tail()
        self.records = self.stable_records()
        return dropped

    def adopt(self, records: list[LogRecord]) -> None:
        """Install an already-assigned record list as the stable log."""

        ordered = list(records)
        seen = set()
        for record in ordered:
            if record.lsn in seen or record.lsn <= 0:
                raise ValueError(f"LSNs must be positive and unique, saw {record.lsn}")
            seen.add(record.lsn)
        ordered.sort(key=lambda record: record.lsn)
        self.records = ordered
        self.stable_upto = ordered[-1].lsn if ordered else 0
        self.next_lsn = (ordered[-1].lsn + 1) if ordered else 1

    def encode_stable(self) -> bytes:
        return encode_records(self.stable_records())


def compensation_counts(records: list[LogRecord]) -> dict[int, int]:
    """How many CLRs name each forward update. Chain CLRs do not count."""

    counts: dict[int, int] = {}
    for record in records:
        if record.kind == "clr" and record.compensates_lsn:
            counts[record.compensates_lsn] = counts.get(record.compensates_lsn, 0) + 1
    return counts
