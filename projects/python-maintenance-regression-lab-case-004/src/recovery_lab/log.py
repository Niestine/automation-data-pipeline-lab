"""Append one checksummed record and force it to stable storage."""

from __future__ import annotations

import os
from pathlib import Path

from recovery_lab.format import LOG_NAME, Record, encode_record, intact_log_bytes
from recovery_lab.trace import emit


def discard_torn_tail(path: Path) -> int:
    """Truncate an incomplete frame at EOF and return the durable length.

    A checksum hole is not truncated; ``intact_log_bytes`` raises and the
    file is left unchanged.
    """
    if not path.exists():
        return 0
    blob = path.read_bytes()
    intact = intact_log_bytes(blob)
    if len(intact) != len(blob):
        with path.open("r+b") as handle:
            handle.truncate(len(intact))
            handle.flush()
            os.fsync(handle.fileno())
    return len(intact)


def force_record(path: Path, record: Record) -> None:
    """Append ``record`` and fsync the log file.

    The caller assigns ``record.lsn``. This is the recovery compensation
    writer. Transaction commit batches its own append in ``LedgerStore``.
    """
    blob = encode_record(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    discard_torn_tail(path)
    with path.open("ab") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    emit(
        "log_append",
        root=path.parent,
        commit_id=record.tx_id,
        lsn=record.lsn,
        schema_version=record.schema_version,
        path=LOG_NAME,
        byte_count=len(blob),
        element_state=record.kind,
    )
    emit(
        "log_flush",
        root=path.parent,
        commit_id=record.tx_id,
        lsn=record.lsn,
        schema_version=record.schema_version,
        path=LOG_NAME,
        byte_count=len(blob),
    )
