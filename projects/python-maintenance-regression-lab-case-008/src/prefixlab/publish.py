"""Single-file replace protocols and the crash classes of their prefixes."""

from __future__ import annotations

import logging

from prefixlab.errors import OracleFault
from prefixlab.fakefs import FakeFS, materialize, reorder_rename_before_data_barrier, replay

logger = logging.getLogger("prefixlab.publish")

OUTCOMES = ("old", "new", "corrupt", "durability_loss", "unspecified")


def _tmp_name(dest: str) -> str:
    return dest + ".tmp"


def safe_publish(fs: FakeFS, dir_id: int, dest: str, new_bytes: bytes) -> None:
    """Pin the directory, fsync the temp, rename onto dest, fsync the directory, then succeed."""

    pinned = fs.pin_dir(dir_id)
    fs.write(_tmp_name(dest), new_bytes)
    fs.fsync(_tmp_name(dest))
    fs.rename(_tmp_name(dest), dest, pinned, pinned=True)
    fs.fsync_dir(pinned)
    fs.mark_success()


def rename_before_data_fsync(fs: FakeFS, dir_id: int, dest: str, new_bytes: bytes) -> None:
    """Unsafe: no data fsync before rename, so the entry can become durable over a torn file."""

    pinned = fs.pin_dir(dir_id)
    fs.write(_tmp_name(dest), new_bytes)
    fs.rename(_tmp_name(dest), dest, pinned, pinned=True)
    fs.fsync_dir(pinned)
    fs.mark_success()


def return_before_dir_fsync(fs: FakeFS, dir_id: int, dest: str, new_bytes: bytes) -> None:
    """Unsafe: success is reported while the renamed entry is not yet durable."""

    pinned = fs.pin_dir(dir_id)
    fs.write(_tmp_name(dest), new_bytes)
    fs.fsync(_tmp_name(dest))
    fs.rename(_tmp_name(dest), dest, pinned, pinned=True)
    fs.mark_success()


def unpinned_publish(fs: FakeFS, dir_id: int, dest: str, new_bytes: bytes) -> None:
    """Resolve the parent by path at rename time. A racing parent rename is unspecified."""

    fs.write(_tmp_name(dest), new_bytes)
    fs.fsync(_tmp_name(dest))
    fs.rename(_tmp_name(dest), dest, dir_id, pinned=False)
    fs.fsync_dir(dir_id, pinned=False)
    fs.mark_success()


def classify(data: bytes | None, old: bytes, new: bytes, *, success: bool, unspecified: bool) -> str:
    if unspecified:
        return "unspecified"
    if data is None or data == old:
        if success:
            return "durability_loss"
        return "old"
    if data == new:
        return "new"
    return "corrupt"


def crash_cuts(fs: FakeFS, dir_id: int, dest: str, old: bytes, new: bytes) -> list[dict]:
    rows = []
    for count in range(0, len(fs.ops) + 1):
        image = replay(fs, count)
        success = any(op.kind == "success" for op in fs.ops[:count])
        data = image.bytes_at(dir_id, dest)
        outcome = classify(data, old, new, success=success, unspecified=image.unspecified)
        rows.append(
            {
                "cut": count,
                "outcome": outcome,
                "transactions": [],
                "located_in": image.located_in(dest),
                "success": success,
            }
        )
    return rows


def scheduled_reorder(fs: FakeFS, dir_id: int, dest: str, old: bytes, new: bytes) -> dict:
    """The one non-prefix case: the issued protocol's rename persists before its data fsync."""

    reordered, cut = reorder_rename_before_data_barrier(fs.ops)
    image = materialize(fs, reordered[:cut])
    outcome = classify(image.bytes_at(dir_id, dest), old, new, success=False, unspecified=image.unspecified)
    return {
        "cut": "publish_rename_before_data_barrier",
        "outcome": outcome,
        "transactions": [],
        "located_in": image.located_in(dest),
        "success": False,
        "persisted": [op.kind for op in reordered[:cut]],
    }


def fault_if_corrupt(cuts: list[dict], outcomes: tuple[str, ...] = ("corrupt",)) -> None:
    """Raise ``OracleFault`` for the first cut whose outcome is in ``outcomes``."""

    for row in cuts:
        if row["outcome"] in outcomes:
            payload = {
                "cut": row["cut"],
                "outcome": row["outcome"],
                "transactions": list(row.get("transactions") or []),
            }
            logger.error(
                "outcome=%s cut=%s transactions=%s",
                payload["outcome"],
                payload["cut"],
                ",".join(payload["transactions"]),
            )
            raise OracleFault(payload)


def mount_shelf(profile: str, old: bytes, sector_size: int = 8, *, race_parent: bool = False) -> FakeFS:
    fs = FakeFS(profile=profile, sector_size=sector_size, race_parent=race_parent)
    fs.mount(1, {"shelf.dat": old})
    fs.mount(2, {})
    return fs
