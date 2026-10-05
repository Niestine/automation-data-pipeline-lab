"""Summary counts the command line prints. Tests assert the same oracles directly."""

from __future__ import annotations

import json
from pathlib import Path

from prefixlab.errors import OracleFault
from prefixlab.history import check_history
from prefixlab.inventory import fault_if_lost, run_both_reads_first, run_exclusive_abort, run_serial
from prefixlab.publish import (
    crash_cuts,
    mount_shelf,
    rename_before_data_fsync,
    return_before_dir_fsync,
    safe_publish,
    scheduled_reorder,
    unpinned_publish,
)
from prefixlab.recover import restart
from prefixlab.txn import Store, apply_script
from prefixlab.wal import compensation_counts


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_json(name: str) -> dict:
    path = project_root() / "examples" / name
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{name} must contain an object")
    return doc


def recovery_audit(script: dict | None = None) -> dict:
    script = script if script is not None else load_json("recovery_script.json")
    store = apply_script(Store(), script)
    initial = dict(store.pages.initial)
    records = list(store.log.records)
    commit_lsn = {
        record.txn: record.lsn for record in records if record.kind == "commit"
    }
    live = {page_id: store.page(page_id) for page_id in initial}
    mismatches = 0
    highest = 0
    appended = 0
    for count in range(0, len(records) + 1):
        prefix = records[:count]
        recovered = restart(initial, prefix)
        got = {page_id: recovered.page(page_id) for page_id in initial}
        for page_id, original in initial.items():
            owner = _owner(script, page_id)
            expected = original
            if owner in commit_lsn and any(record.lsn == commit_lsn[owner] for record in prefix):
                expected = live[page_id]
            if got[page_id] != expected:
                mismatches += 1
        counts = compensation_counts(recovered.log.records)
        if counts:
            highest = max(highest, max(counts.values()))
        again = restart(initial, recovered.log.records)
        if len(again.log.records) != len(recovered.log.records):
            appended += len(again.log.records) - len(recovered.log.records)
        if compensation_counts(again.log.records) != counts:
            mismatches += 1
    return {
        "prefixes": len(records) + 1,
        "mismatches": mismatches,
        "max_compensations": highest,
        "double_restart_extra": appended,
        "stable_records": len(store.log.stable_records()),
        "logical_records": len(records),
    }


def _owner(script: dict, page_id: str) -> str | None:
    owner = None
    for step in script["steps"]:
        if step.get("op") == "update" and step.get("page") == page_id:
            owner = step["txn"]
    return owner


def publish_audit(shelf: dict | None = None) -> dict:
    shelf = shelf if shelf is not None else load_json("shelf_bytes.json")
    old = shelf["old"].encode("utf-8")
    new = shelf["new"].encode("utf-8")
    sector = int(shelf.get("sector_size", 8))
    safe = _cuts("ordered_atomic", safe_publish, old, new, sector)
    early = _cuts("ordered_atomic", rename_before_data_fsync, old, new, sector)
    durable = _cuts("ordered_atomic", return_before_dir_fsync, old, new, sector)
    relaxed = _cuts("relaxed", safe_publish, old, new, sector)
    reorder_fs = mount_shelf("ordered_atomic", old, sector)
    safe_publish(reorder_fs, 1, "shelf.dat", new)
    reorder = scheduled_reorder(reorder_fs, 1, "shelf.dat", old, new)
    pinned = _race(safe_publish, old, new, sector)
    unpinned = _race(unpinned_publish, old, new, sector)
    return {
        "safe_corrupt": _count(safe, "corrupt"),
        "safe_after_success": sorted({row["outcome"] for row in safe if row["success"]}),
        "rename_before_corrupt_cuts": [row["cut"] for row in early if row["outcome"] == "corrupt"],
        "durability_loss_cuts": [row["cut"] for row in durable if row["outcome"] == "durability_loss"],
        "reorder_outcome": reorder["outcome"],
        "relaxed_corrupt": _count(relaxed, "corrupt"),
        "pinned_outcomes": sorted({row["outcome"] for row in pinned}),
        "pinned_dirs": sorted({tuple(row["located_in"]) for row in pinned}),
        "unpinned_final": unpinned[-1]["outcome"],
    }


def _cuts(profile: str, protocol, old: bytes, new: bytes, sector: int) -> list[dict]:
    fs = mount_shelf(profile, old, sector)
    protocol(fs, 1, "shelf.dat", new)
    return crash_cuts(fs, 1, "shelf.dat", old, new)


def _race(protocol, old: bytes, new: bytes, sector: int) -> list[dict]:
    fs = mount_shelf("ordered_atomic", old, sector, race_parent=True)
    protocol(fs, 1, "shelf.dat", new)
    return crash_cuts(fs, 1, "shelf.dat", old, new)


def _count(rows: list[dict], outcome: str) -> int:
    return sum(1 for row in rows if row["outcome"] == outcome)


def inventory_audit() -> dict:
    serial = run_serial("exclusive")
    nolock = run_both_reads_first("nolock")
    read_committed = run_both_reads_first("txn_read_committed")
    exclusive = run_both_reads_first("exclusive")
    aborted, _while_locked, views = run_exclusive_abort()
    lost = None
    try:
        fault_if_lost(nolock)
    except OracleFault as exc:
        lost = exc.payload
    return {
        "serial_sum": serial.stock.total,
        "nolock_sum": nolock.stock.total,
        "read_committed_sum": read_committed.stock.total,
        "exclusive_sum": exclusive.stock.total,
        "abort_sum": aborted.stock.total,
        "abort_tokens": list(aborted.stock.tokens),
        "holder_saw_token": views["holder"]["tokens"],
        "reader_blocked": _while_locked["blocked"],
        "lost_outcome": None if lost is None else lost["outcome"],
        "lost_missing": [] if lost is None else lost["missing"],
        "checker_serial": check_history(serial.history())["anomaly"],
    }


def collect() -> dict:
    recovery = recovery_audit()
    publish = publish_audit()
    stock = inventory_audit()
    histories = load_json("histories.json")
    anomalies = {name: check_history(doc)["anomaly"] for name, doc in histories.items()}
    return {
        "recovery_mismatches": recovery["mismatches"],
        "recovery_prefixes": recovery["prefixes"],
        "max_compensations": recovery["max_compensations"],
        "double_restart_extra": recovery["double_restart_extra"],
        "safe_corrupt": publish["safe_corrupt"],
        "reorder_outcome": publish["reorder_outcome"],
        "relaxed_corrupt": publish["relaxed_corrupt"],
        "nolock_sum": stock["nolock_sum"],
        "exclusive_sum": stock["exclusive_sum"],
        "anomalies": anomalies,
    }
