"""Measured crash images. Counts printed by ``run_lab.py metrics`` come from here."""

from __future__ import annotations

import math
import shutil
import struct
import tempfile
from pathlib import Path

from recovery_lab.errors import (
    SimulatedCrash,
    StructuralCorruption,
    UnavailableSchemaGap,
)
from recovery_lab.faults import (
    Trace,
    block_cuts,
    first_failure_rank,
    materialize,
    rank_cuts,
    score_cut,
    write_image,
)
from recovery_lab.format import LOG_NAME, MIN_PAYLOAD, SNAP_NAME, crc32, encode_record, parse_log
from recovery_lab.migrate import Migrator, integrity
from recovery_lab.outcomes import classify
from recovery_lab.recover import UndoInjector, directory_hashes, recover
from recovery_lab.snapshot import decode_snapshot, encode_snapshot, fingerprint, read_snapshot
from recovery_lab.store import LedgerStore

ACCOUNT_EMAIL = "a@example.com"


def two_commit_trace(*, publish_before_log_flush: bool = False) -> Trace:
    """Account 1 balance 10, then balance 25. The trace covers both commits."""
    with tempfile.TemporaryDirectory(prefix="rl04-trace-") as tmp:
        with LedgerStore(tmp, publish_before_log_flush=publish_before_log_flush) as store:
            store.upsert(1, 10, ACCOUNT_EMAIL)
            store.commit()
            store.upsert(1, 25, ACCOUNT_EMAIL)
            store.commit()
            return store.trace


def analyze(trace: Trace, block: int, *, code_version: int = 1) -> list[dict]:
    """Classify every ranked Model Z cut. One row per cut, in rank order."""
    ops = trace.ops
    rows: list[dict] = []
    for cut in rank_cuts(ops, block_cuts(ops, block)):
        try:
            files = materialize(ops, cut)
        except StructuralCorruption as exc:
            rows.append(
                {
                    "outcome": classify(None, [], "", exc),
                    "last_durable_lsn": exc.last_durable_lsn,
                    "balance": None,
                    "cut": cut,
                    "score": score_cut(ops, cut),
                }
            )
            continue
        with tempfile.TemporaryDirectory(prefix="rl04-img-") as tmp:
            write_image(tmp, files)
            try:
                result = recover(tmp, code_version=code_version)
            except (StructuralCorruption, UnavailableSchemaGap) as exc:
                rows.append(
                    {
                        "outcome": classify(None, [], "", exc),
                        "last_durable_lsn": exc.last_durable_lsn,
                        "balance": None,
                        "cut": cut,
                        "score": score_cut(ops, cut),
                    }
                )
            else:
                account = result.ledger.accounts.get(1)
                rows.append(
                    {
                        "outcome": result.outcome,
                        "last_durable_lsn": result.last_durable_lsn,
                        "balance": None if account is None else account["balance"],
                        "fingerprint": result.fingerprint,
                        "cut": cut,
                        "score": score_cut(ops, cut),
                    }
                )
    return rows


def commit_cut_balances(trace: Trace, block: int, commit_ordinal: int) -> list[dict]:
    """Recover each cut inside one commit record. ``commit_ordinal`` is 0-based."""
    ops = trace.ops
    commits = [op.index for op in ops if op.role == "log_commit"]
    target = commits[commit_ordinal]
    found: list[dict] = []
    for cut in block_cuts(ops, block):
        if cut.op_index != target:
            continue
        if not (0 <= cut.offset < len(ops[target].data)):
            continue
        files = materialize(ops, cut)
        with tempfile.TemporaryDirectory(prefix="rl04-commit-") as tmp:
            write_image(tmp, files)
            result = recover(tmp)
        found.append(
            {
                "outcome": result.outcome,
                "balance": _balance(result),
                "offset": cut.offset,
            }
        )
    return found


def _balance(result) -> int | None:
    account = result.ledger.accounts.get(1)
    return None if account is None else account["balance"]


def undo_compensation_count() -> int:
    """Ten crashes at the first undo step, one at the second, then a clean recover."""
    with tempfile.TemporaryDirectory(prefix="rl04-undo-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            store.upsert(1, 10, ACCOUNT_EMAIL)
            store.commit()
            pre = fingerprint(store.ledger)
            store.upsert(1, 25, ACCOUNT_EMAIL)
            store.upsert(2, 5, "b@example.com")
            store.durabilize_open_transaction()
        injector = UndoInjector()
        injector.crash_budget[1] = 10
        log_path = root / LOG_NAME
        size = log_path.stat().st_size
        for _ in range(10):
            try:
                recover(root, injector=injector)
            except SimulatedCrash:
                pass
            else:
                raise AssertionError("undo step 1 did not crash")
            if log_path.stat().st_size != size:
                raise AssertionError("repeated undo crash grew the log")
        injector.crash_budget[2] = 1
        try:
            recover(root, injector=injector)
        except SimulatedCrash:
            pass
        else:
            raise AssertionError("undo step 2 did not crash")
        result = recover(root, injector=injector)
        if result.fingerprint != pre:
            raise AssertionError("undo did not restore the pre-transaction ledger")
        if result.compensation_count != 2:
            raise AssertionError(result.compensation_count)
        return result.compensation_count


def illegal_orphan_count() -> int:
    """Public insert followed by an absent-state delete leaves an index key."""
    with tempfile.TemporaryDirectory(prefix="rl04-orphan-") as tmp:
        with LedgerStore(tmp) as writer:
            writer.upsert(1, 10, ACCOUNT_EMAIL)
            writer.commit()
        with LedgerStore(tmp, email_state="absent") as deleter:
            deleter.delete(1)
            deleter.commit()
        with LedgerStore(tmp) as reader:
            reader._load_idle()
            report = integrity(reader.ledger, email_public=True, status_public=False)
        return report.orphan_index_keys


def _forbid(store: LedgerStore, *names: str) -> None:
    def blocked(name: str):
        def _call(*_args, **_kwargs):
            raise AssertionError(f"{name} called while the element is not public")

        return _call

    for name in names:
        setattr(store, name, blocked(name))


def _interleave(left: LedgerStore, right: LedgerStore) -> None:
    left.upsert(10, 10, "left@example.com")
    left.commit()
    right.upsert(10, 15, "left@example.com")
    right.commit()
    left.upsert(11, 4, "row@example.com")
    left.commit()
    right.delete(11)
    right.commit()
    left.upsert(12, 8, "third@example.com")
    left.commit()
    right.upsert(10, 16, "moved@example.com")
    right.commit()
    left._load_idle()
    right._load_idle()


def _pair(directory: Path, older: int, newer: int, *, email_public: bool, status_public: bool) -> dict:
    with LedgerStore(directory, schema_version=older) as left, LedgerStore(
        directory, schema_version=newer
    ) as right:
        if not email_public:
            _forbid(left, "read_email")
            _forbid(right, "read_email")
        elif older < 6 and newer >= 6:
            _forbid(right, "read_email")
        if not status_public:
            _forbid(left, "read_status")
            _forbid(right, "read_status")
        elif older < 5:
            _forbid(left, "read_status")
        violations = 0
        try:
            _interleave(left, right)
        except AssertionError:
            violations += 1
        email_hit = None
        if email_public and older < 6:
            email_hit = left.read_email("moved@example.com")
        status_hit = None
        if status_public and newer >= 5:
            status_hit = right.read_status(10)
        left._load_idle()
        report = integrity(left.ledger, email_public=email_public, status_public=status_public)
        return {
            "older": older,
            "newer": newer,
            "orphans": report.orphan_index_keys,
            "missing": report.missing_public_index,
            "status_missing": report.rows_missing_status,
            "illegal_reads": left.illegal_reads + right.illegal_reads,
            "reader_violations": violations,
            "email_hit": email_hit,
            "status_hit": status_hit,
        }


def neighbor_report() -> dict:
    """Six neighbor pairs. Versions 5 and 9 are refused until the gate commit is durable."""
    pairs: list[dict] = []
    blocked_before_backfill = False
    blocked_before_cleanup = False
    with tempfile.TemporaryDirectory(prefix="rl04-pair12-") as tmp:
        root = Path(tmp)
        pairs.append(_pair(root, 1, 2, email_public=True, status_public=False))
    with tempfile.TemporaryDirectory(prefix="rl04-pair23-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            Migrator(store).advance_to(2)
        pairs.append(_pair(root, 2, 3, email_public=True, status_public=False))
    with tempfile.TemporaryDirectory(prefix="rl04-pair35-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            store.upsert(1, 3, "seed@example.com")
            store.commit()
            Migrator(store).advance_to(3)
        try:
            opened = LedgerStore(root, schema_version=5)
        except UnavailableSchemaGap:
            blocked_before_backfill = True
        else:
            opened.close()
        with LedgerStore(root, schema_version=3) as store:
            Migrator(store).advance_to(4)
        pairs.append(_pair(root, 3, 5, email_public=True, status_public=True))
    with tempfile.TemporaryDirectory(prefix="rl04-pair56-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            store.upsert(1, 5, "seed@example.com")
            store.commit()
            Migrator(store).advance_to(5)
        pairs.append(_pair(root, 5, 6, email_public=True, status_public=True))
    with tempfile.TemporaryDirectory(prefix="rl04-pair67-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            store.upsert(1, 6, "seed@example.com")
            store.commit()
            Migrator(store).advance_to(6)
        pairs.append(_pair(root, 6, 7, email_public=False, status_public=True))
    with tempfile.TemporaryDirectory(prefix="rl04-pair79-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            store.upsert(1, 7, "seed@example.com")
            store.commit()
            Migrator(store).advance_to(7)
        try:
            opened = LedgerStore(root, schema_version=9)
        except UnavailableSchemaGap:
            blocked_before_cleanup = True
        else:
            opened.close()
        with LedgerStore(root, schema_version=7) as store:
            Migrator(store).advance_to(8)
        pairs.append(_pair(root, 7, 9, email_public=False, status_public=True))
    return {
        "pairs": pairs,
        "orphan_total": sum(row["orphans"] for row in pairs),
        "blocked_before_backfill": blocked_before_backfill,
        "blocked_before_cleanup": blocked_before_cleanup,
    }


def _install(directory: Path, log_blob: bytes, snap_blob: bytes | None, extra: dict[str, bytes] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / LOG_NAME).write_bytes(log_blob)
    snap_path = directory / SNAP_NAME
    if snap_blob is None:
        if snap_path.exists():
            snap_path.unlink()
    else:
        snap_path.write_bytes(snap_blob)
    for name, blob in (extra or {}).items():
        (directory / name).write_bytes(blob)


def model_p_images() -> list[dict]:
    """Garbage sector, size-before-data without a log flush, and the same image with the log flushed."""
    with tempfile.TemporaryDirectory(prefix="rl04-modelp-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            store.upsert(1, 10, ACCOUNT_EMAIL)
            store.commit()
            log1 = (root / LOG_NAME).read_bytes()
            snap1 = (root / SNAP_NAME).read_bytes()
            store.upsert(1, 25, "account.two@example.com")
            store.commit()
            log2 = (root / LOG_NAME).read_bytes()
            snap2 = (root / SNAP_NAME).read_bytes()
            store.upsert(1, 25, ("x" * 900) + "@example.com")
            store.durabilize_open_transaction()
            long_log = (root / LOG_NAME).read_bytes()
    if len(snap2) <= len(snap1):
        raise AssertionError("second snapshot did not grow")
    padded = snap1 + (b"\x00" * (len(snap2) - len(snap1)))
    if decode_snapshot(padded) is not None:
        raise AssertionError("zero-extended snapshot decoded")
    frames = _frames(long_log)
    start, end = frames[-1]
    if not (end == len(long_log) and start < len(long_log) - 512):
        raise AssertionError("garbage window does not sit inside a framed record")
    garbage = bytearray(long_log)
    garbage[-512:] = b"\x58" * 512
    rows = []
    with tempfile.TemporaryDirectory(prefix="rl04-garbage-") as tmp:
        root = Path(tmp)
        _install(root, bytes(garbage), snap1)
        before = directory_hashes(root)
        try:
            recover(root)
        except StructuralCorruption as exc:
            rows.append(
                {
                    "name": "garbage",
                    "outcome": "structural_corruption",
                    "last_durable_lsn": exc.last_durable_lsn,
                    "unchanged": directory_hashes(root) == before,
                }
            )
        else:
            raise AssertionError("garbage sector did not raise")
    with tempfile.TemporaryDirectory(prefix="rl04-size-a-") as tmp:
        root = Path(tmp)
        _install(root, log1, padded)
        result = recover(root)
        rows.append(
            {
                "name": "size_before_data_unflushed",
                "outcome": result.outcome,
                "balance": _balance(result),
            }
        )
    with tempfile.TemporaryDirectory(prefix="rl04-size-b-") as tmp:
        root = Path(tmp)
        _install(root, log2, padded)
        result = recover(root)
        rows.append(
            {
                "name": "size_before_data_flushed",
                "outcome": result.outcome,
                "balance": _balance(result),
            }
        )
    return rows


def rename_images() -> list[dict]:
    """Both names, neither name, and temp-only, each with a flushed log."""
    with tempfile.TemporaryDirectory(prefix="rl04-rename-src-") as tmp:
        root = Path(tmp)
        with LedgerStore(root) as store:
            store.upsert(1, 10, ACCOUNT_EMAIL)
            store.commit()
            store.upsert(1, 25, ACCOUNT_EMAIL)
            store.commit()
        log_blob = (root / LOG_NAME).read_bytes()
        snap_blob = (root / SNAP_NAME).read_bytes()
    temp_name = ".ledger.snap.tmp-rename"
    specs = (
        ("both_names", snap_blob, {temp_name: snap_blob}),
        ("neither_name", None, {}),
        ("temp_only", None, {temp_name: snap_blob}),
    )
    rows = []
    for name, snap, extra in specs:
        with tempfile.TemporaryDirectory(prefix="rl04-rename-") as tmp:
            root = Path(tmp)
            _install(root, log_blob, snap, extra)
            result = recover(root)
            rows.append({"name": name, "outcome": result.outcome, "balance": _balance(result)})
    return rows


def _frames(blob: bytes) -> list[tuple[int, int]]:
    frames: list[tuple[int, int]] = []
    index = 0
    while index + 4 <= len(blob):
        (length,) = struct.unpack_from("<I", blob, index)
        frame_end = index + 4 + length
        if length < MIN_PAYLOAD or frame_end > len(blob):
            break
        frames.append((index, frame_end))
        index = frame_end
    return frames


def patch_kind(blob: bytes, frame_index: int, kind_code: int) -> bytes:
    """Rewrite one record's kind byte and its CRC. The frame length stays put."""
    start, end = _frames(blob)[frame_index]
    payload = bytearray(blob[start + 4 : end])
    payload[24] = kind_code & 0xFF
    body = bytes(payload[:-4])
    payload[-4:] = struct.pack("<I", crc32(body))
    out = bytearray(blob)
    out[start + 4 : end] = payload
    return bytes(out)


def rewrite_schema_version(directory: Path, version: int) -> None:
    ledger, _blob = read_snapshot(directory)
    if ledger is None:
        raise AssertionError("snapshot missing")
    ledger.schema_version = version
    (directory / SNAP_NAME).write_bytes(encode_snapshot(ledger))


def rewrite_checkpoint_lsn(directory: Path, lsn: int) -> None:
    path = directory / LOG_NAME
    records = parse_log(path.read_bytes())
    rewritten = []
    for record in records:
        if record.kind == "checkpoint":
            record.lsn = lsn
            if record.after is not None:
                record.after = dict(record.after)
                record.after["hint_lsn"] = lsn
        rewritten.append(encode_record(record))
    path.write_bytes(b"".join(rewritten))


def drop_checkpoint_records(directory: Path) -> None:
    path = directory / LOG_NAME
    records = [record for record in parse_log(path.read_bytes()) if record.kind != "checkpoint"]
    path.write_bytes(b"".join(encode_record(record) for record in records))


def copy_directory(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest)


def collect() -> dict:
    """Run the measured set and return plain numbers. No machine paths."""
    correct = two_commit_trace(publish_before_log_flush=False)
    bad = two_commit_trace(publish_before_log_flush=True)
    z512 = analyze(correct, 512)
    z4096 = analyze(correct, 4096)
    bad_rows = analyze(bad, 512)
    classes = [row["outcome"] for row in bad_rows]
    rank = first_failure_rank(classes)
    limit = math.ceil(0.05 * len(bad_rows)) if bad_rows else 0
    renames = rename_images()
    model_p = model_p_images()
    neighbors = neighbor_report()
    compensation = undo_compensation_count()
    orphans = illegal_orphan_count()
    return {
        "permutation_count": len(z512) + len(z4096) + len(bad_rows) + len(renames) + len(model_p),
        "model_z_cut_count": len(z512) + len(z4096),
        "model_z_512_count": len(z512),
        "model_z_4096_count": len(z4096),
        "model_z_all_durable_match": all(row["outcome"] == "durable_match" for row in z512 + z4096),
        "publish_before_flush_cut_count": len(bad_rows),
        "first_failure_rank": rank,
        "first_failure_limit": limit,
        "first_failure_class": None if rank is None else classes[rank - 1],
        "first_failure_score": None if rank is None else bad_rows[rank - 1]["score"],
        "compensation_count": compensation,
        "illegal_orphan_count": orphans,
        "staged_orphan_count": neighbors["orphan_total"],
        "rename_permutation_count": len(renames),
        "model_p_image_count": len(model_p),
        "rename_all_durable_match": all(row["outcome"] == "durable_match" and row["balance"] == 25 for row in renames),
        "model_p_garbage_structural": model_p[0]["outcome"] == "structural_corruption" and model_p[0]["unchanged"],
        "model_p_unflushed_balance": model_p[1]["balance"],
        "model_p_flushed_balance": model_p[2]["balance"],
    }
