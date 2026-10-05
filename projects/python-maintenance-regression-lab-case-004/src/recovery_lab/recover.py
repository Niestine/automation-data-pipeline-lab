"""Restart: redo records past the snapshot LSN, then undo loser transactions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from recovery_lab.errors import (
    SimulatedCrash,
    StructuralCorruption,
    UnavailableSchemaGap,
)
from recovery_lab.format import LOG_NAME, SNAP_NAME, Record, parse_log
from recovery_lab.log import force_record
from recovery_lab.outcomes import UNAVAILABLE_GAP, STRUCTURAL_CORRUPTION, classify
from recovery_lab.publish import publish_file
from recovery_lab.snapshot import (
    Ledger,
    apply_forward,
    apply_image,
    empty_ledger,
    encode_snapshot,
    fingerprint,
    read_snapshot,
)
from recovery_lab.trace import emit


@dataclass
class RecoverResult:
    ledger: Ledger
    outcome: str
    last_durable_lsn: int
    apply_count: int
    applied_lsn: int
    compensation_count: int

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.ledger)


class UndoInjector:
    """Crash budget keyed by the undo step inside a single ``recover`` call.

    The step counter starts at 1 on every call. A crash is raised before the
    compensation record is appended, so a repeated crash does not grow the log.
    """

    def __init__(self) -> None:
        self.crash_budget: dict[int, int] = {}

    def should_crash(self, step: int) -> bool:
        left = self.crash_budget.get(step, 0)
        if left <= 0:
            return False
        self.crash_budget[step] = left - 1
        return True


def _filter_unknown(records: list[Record], *, code_version: int, last_lsn: int) -> list[Record]:
    last_commit = -1
    for index, record in enumerate(records):
        if record.kind == "commit":
            last_commit = index
    for index, record in enumerate(records):
        if not record.is_unknown:
            continue
        if index <= last_commit:
            raise UnavailableSchemaGap(
                f"unknown log kind {record.kind} is required to reach a durable commit",
                code_version=code_version,
                stored_version=record.schema_version,
                last_durable_lsn=last_lsn,
            )
        return records[:index]
    return records


def _commit_hashes(records: list[Record]) -> list[str]:
    ledger = empty_ledger()
    hashes = [fingerprint(ledger)]
    for record in records:
        if record.kind == "checkpoint" or record.is_unknown:
            continue
        apply_forward(ledger, record)
        if record.kind == "commit":
            hashes.append(fingerprint(ledger))
    return hashes


def _oracle(records: list[Record]) -> Ledger:
    scratch = list(records)
    ledger = empty_ledger()
    for record in scratch:
        if record.kind == "checkpoint" or record.is_unknown:
            continue
        apply_forward(ledger, record)
    _undo(ledger, scratch, writer=None, injector=None, counter=None)
    return ledger


def _loser_ids(records: list[Record]) -> list[int]:
    updates: dict[int, list[Record]] = {}
    committed: set[int] = set()
    compensated: set[int] = set()
    for record in records:
        if record.kind == "commit":
            committed.add(record.tx_id)
        elif record.kind == "update":
            updates.setdefault(record.tx_id, []).append(record)
        elif record.kind == "compensation":
            compensated.add(record.undone_lsn)
    losers: list[int] = []
    for tx_id, rows in updates.items():
        if tx_id in committed or tx_id == 0:
            continue
        if all(row.lsn in compensated for row in rows):
            continue
        losers.append(tx_id)
    return losers


def _undo(
    ledger: Ledger,
    records: list[Record],
    *,
    writer,
    injector: UndoInjector | None,
    counter: list[int] | None,
) -> int:
    """Undo loser transactions. Returns how many compensation records were forced."""
    by_lsn = {record.lsn: record for record in records}
    root = writer.path.parent if writer is not None else None
    written = 0
    for tx_id in _loser_ids(records):
        updates = [record for record in records if record.kind == "update" and record.tx_id == tx_id]
        if not updates:
            continue
        undone = {
            record.undone_lsn: record
            for record in records
            if record.kind == "compensation" and record.tx_id == tx_id
        }
        walk: Record | None = updates[-1]
        step = 0
        while walk is not None:
            existing = undone.get(walk.lsn)
            if existing is not None:
                nxt = existing.undo_next_lsn
                walk = by_lsn.get(nxt) if nxt else None
                if walk is not None and walk.kind != "update":
                    walk = None
                continue
            step += 1
            if injector is not None and injector.should_crash(step):
                raise SimulatedCrash(f"crash before compensation force at undo step {step}")
            comp = Record(
                lsn=0,
                prev_lsn=walk.lsn,
                tx_id=walk.tx_id,
                kind="compensation",
                schema_version=walk.schema_version,
                undo_next_lsn=walk.prev_lsn,
                undone_lsn=walk.lsn,
                before=walk.after,
                after=walk.before,
            )
            if writer is not None:
                comp = writer(comp)
                written += 1
                emit(
                    "compensate",
                    root=root,
                    commit_id=walk.tx_id,
                    lsn=comp.lsn,
                    schema_version=walk.schema_version,
                    byte_count=0,
                )
            apply_image(ledger, walk.before)
            if counter is not None:
                counter[0] += 1
            if comp.lsn:
                ledger.applied_lsn = comp.lsn
            records.append(comp)
            by_lsn[comp.lsn] = comp
            undone[walk.lsn] = comp
            if writer is not None:
                emit(
                    "undo",
                    root=root,
                    commit_id=walk.tx_id,
                    lsn=comp.lsn,
                    schema_version=ledger.schema_version,
                )
            nxt = walk.prev_lsn
            walk = by_lsn.get(nxt) if nxt else None
            if walk is not None and walk.kind != "update":
                walk = None
    return written


class _LogAppender:
    def __init__(self, path: Path, next_lsn: int) -> None:
        self.path = path
        self.next_lsn = next_lsn

    def __call__(self, record: Record) -> Record:
        record.lsn = self.next_lsn
        self.next_lsn += 1
        force_record(self.path, record)
        return record


def _prepare(directory, code_version: int):
    """Read a snapshot and an intact log. Redo. Do not undo or publish.

    A CRC-valid snapshot is kept even when its LSN is ahead of the log.
    A checksum failure on the snapshot discards those bytes and replays
    from an empty ledger. A CRC hole or a schema gap raises and writes
    nothing.
    """
    root = Path(directory)
    snap_ledger, _snap_blob = read_snapshot(root)
    log_path = root / LOG_NAME
    log_blob = log_path.read_bytes() if log_path.exists() else b""
    all_records = parse_log(log_blob)
    last_durable_lsn = all_records[-1].lsn if all_records else 0
    records = _filter_unknown(all_records, code_version=code_version, last_lsn=last_durable_lsn)
    future_tail = len(records) != len(all_records)
    last_durable_lsn = records[-1].lsn if records else 0
    if snap_ledger is None:
        stored_version = 1
        ledger = empty_ledger()
    else:
        stored_version = snap_ledger.schema_version
        ledger = snap_ledger.copy()
    if abs(code_version - stored_version) > 1:
        raise UnavailableSchemaGap(
            f"code schema {code_version} is more than one version from stored schema {stored_version}",
            code_version=code_version,
            stored_version=stored_version,
            last_durable_lsn=last_durable_lsn,
        )
    for record in records:
        if record.kind == "checkpoint" or record.is_unknown:
            continue
        if record.lsn <= ledger.applied_lsn:
            continue
        apply_forward(ledger, record)
    return ledger, records, last_durable_lsn, log_path, future_tail


def load_ledger(directory, *, code_version: int = 1) -> Ledger:
    """Replay the durable prefix into memory. Does not undo or publish."""
    ledger, _records, _lsn, _path, _future = _prepare(directory, code_version)
    return ledger


def _emit_outcome(root: Path, outcome: str, lsn: int, schema_version: int) -> None:
    emit(
        "outcome",
        root=root,
        outcome=outcome,
        lsn=lsn,
        schema_version=schema_version,
        commit_id=0,
        path=LOG_NAME,
        byte_count=0,
    )


def recover(
    directory,
    *,
    code_version: int = 1,
    injector: UndoInjector | None = None,
) -> RecoverResult:
    """Recover ``directory``.

    Does not read ``audit.log``. A torn log tail is dropped. A CRC hole or a
    schema gap leaves every file byte unchanged. A finished call emits one
    outcome record. ``SimulatedCrash`` propagates with no outcome and no
    snapshot publish.

    A log tail of record kinds this process cannot read is left in place.
    Undo would have to append compensation records behind that tail, which
    the next open could not get past, so a loser transaction plus a future
    tail raises ``UnavailableSchemaGap`` and writes nothing.
    """
    root = Path(directory)
    try:
        ledger, records, last_durable_lsn, log_path, future_tail = _prepare(root, code_version)
        if future_tail and _loser_ids(records):
            raise UnavailableSchemaGap(
                "undo would append behind a log record this process cannot read",
                code_version=code_version,
                stored_version=ledger.schema_version,
                last_durable_lsn=last_durable_lsn,
            )
    except StructuralCorruption as exc:
        _emit_outcome(root, STRUCTURAL_CORRUPTION, exc.last_durable_lsn, code_version)
        raise
    except UnavailableSchemaGap as exc:
        _emit_outcome(root, UNAVAILABLE_GAP, exc.last_durable_lsn, code_version)
        raise
    # ``_prepare`` already redid. Count the row images it applied with the
    # same rule (LSN past the snapshot cursor) instead of redoing twice.
    snap_cursor = _snapshot_cursor(root)
    apply_count = 0
    for record in records:
        if record.kind == "checkpoint" or record.is_unknown:
            continue
        if record.lsn <= snap_cursor:
            continue
        if record.kind in ("update", "compensation"):
            apply_count += 1
            emit(
                "redo",
                root=root,
                commit_id=record.tx_id,
                lsn=record.lsn,
                schema_version=ledger.schema_version,
                path=LOG_NAME,
            )
    correct = _oracle(records)
    correct_hash = fingerprint(correct)
    boundaries = _commit_hashes(records)
    counter = [0]
    next_lsn = (records[-1].lsn + 1) if records else 1
    appender = _LogAppender(log_path, next_lsn)
    _undo(ledger, records, writer=appender, injector=injector, counter=counter)
    apply_count += counter[0]
    outcome = classify(fingerprint(ledger), boundaries, correct_hash, None)
    publish_file(root / SNAP_NAME, encode_snapshot(ledger), lsn=ledger.applied_lsn)
    durable_records = parse_log(log_path.read_bytes() if log_path.exists() else b"")
    compensation_count = sum(1 for record in durable_records if record.kind == "compensation")
    last_durable_lsn = durable_records[-1].lsn if durable_records else 0
    _emit_outcome(root, outcome, last_durable_lsn, ledger.schema_version)
    return RecoverResult(
        ledger=ledger,
        outcome=outcome,
        last_durable_lsn=last_durable_lsn,
        apply_count=apply_count,
        applied_lsn=ledger.applied_lsn,
        compensation_count=compensation_count,
    )


def _snapshot_cursor(root: Path) -> int:
    snap, _blob = read_snapshot(root)
    if snap is None:
        return 0
    return snap.applied_lsn


def directory_hashes(directory) -> tuple[str, str]:
    from recovery_lab.snapshot import file_hash

    root = Path(directory)
    return file_hash(root / SNAP_NAME), file_hash(root / LOG_NAME)
