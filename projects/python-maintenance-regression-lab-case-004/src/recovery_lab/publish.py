"""Same-directory publish: temp file, file flush, os.replace, directory flush."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from recovery_lab.errors import SameVolumeRequired
from recovery_lab.trace import emit

StatFn = Callable[[Path], os.stat_result]
ReplaceFn = Callable[[Path, Path], None]


@dataclass(frozen=True)
class CleanupEntry:
    kind: str
    path: str


def plan_deferred_cleanup(directory: str, files: list[str]) -> list[CleanupEntry]:
    """Order a simulated boot cleanup: every child file, then the directory.

    ``MoveFileExW`` runs ``MOVEFILE_DELAY_UNTIL_REBOOT`` entries in caller
    order and will not remove a directory that still contains files. This
    lab does not register a boot-time delete. The helper only builds the
    order a regression can check.
    """
    entries = [CleanupEntry("file", name) for name in files]
    entries.append(CleanupEntry("dir", directory))
    return entries


def files_before_directory(entries: list[CleanupEntry]) -> bool:
    directory_at: int | None = None
    for index, entry in enumerate(entries):
        if entry.kind == "dir":
            if directory_at is not None:
                return False
            directory_at = index
        elif entry.kind == "file":
            if directory_at is not None:
                return False
        else:
            return False
    return directory_at is not None


def _device(path: Path, stat_fn: StatFn) -> int:
    try:
        return int(stat_fn(path).st_dev)
    except OSError:
        return int(stat_fn(path.parent).st_dev)


def temp_path(dest: Path, lsn: int) -> Path:
    """Temp name beside ``dest``: ``.{name}.tmp-{pid}-{lsn}``."""
    return dest.parent / f".{dest.name}.tmp-{os.getpid()}-{lsn}"


def assert_same_volume(dest: Path, *, lsn: int, stat_fn: StatFn | None = None) -> Path:
    """Refuse a temp file that does not share ``dest``'s device.

    The check runs before ``dest`` is opened for write or delete. A missing
    temp path is stated through its parent, which is the directory that will
    hold it.
    """
    dest = Path(dest)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp = temp_path(dest, lsn)
    stat = stat_fn or os.stat
    if _device(temp, stat) != _device(parent, stat):
        raise SameVolumeRequired(f"{temp} and {dest} are on different devices")
    return temp


def directory_flush(parent: Path) -> bool:
    """fsync the parent directory. Return False when the OS refuses the handle."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(parent), flags)
    except OSError:
        return False
    try:
        try:
            os.fsync(fd)
        except OSError:
            return False
    finally:
        os.close(fd)
    return True


def publish_file(
    dest: Path,
    payload: bytes,
    *,
    lsn: int,
    stat_fn: StatFn | None = None,
    replace_fn: ReplaceFn | None = None,
) -> Path:
    """Publish ``payload`` as ``dest`` without writing the destination in place.

    The temp file is created in ``dest``'s parent. Device numbers are compared
    before the destination is opened. ``os.replace`` is the publish call.
    A failed replace leaves ``dest`` as it was.
    """
    dest = Path(dest)
    parent = dest.parent
    temp = assert_same_volume(dest, lsn=lsn, stat_fn=stat_fn)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(temp), flags, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    emit(
        "snapshot_write",
        root=parent,
        lsn=lsn,
        path=str(temp.name),
        byte_count=len(payload),
    )
    emit(
        "snapshot_flush",
        root=parent,
        lsn=lsn,
        path=str(temp.name),
        byte_count=len(payload),
    )
    replace = replace_fn or os.replace
    try:
        replace(temp, dest)
    except OSError:
        emit(
            "replace",
            root=parent,
            lsn=lsn,
            path=str(dest.name),
            byte_count=len(payload),
            outcome="oserror",
        )
        raise
    emit(
        "replace",
        root=parent,
        lsn=lsn,
        path=str(dest.name),
        byte_count=len(payload),
        outcome="ok",
    )
    supported = directory_flush(parent)
    emit(
        "dir_flush",
        root=parent,
        lsn=lsn,
        path=str(parent),
        byte_count=0,
        element_state="supported" if supported else "unsupported",
    )
    if temp.exists():
        temp.unlink()
    return temp
