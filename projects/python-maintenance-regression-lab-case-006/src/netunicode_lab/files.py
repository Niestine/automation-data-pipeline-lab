"""Pathlib creates, case-fold checks, and close-then-replace writes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import CasefoldCollisionError


def reject_casefold_collision(path: Path) -> None:
    """Raise when another directory entry casefolds to the same name.

    The check runs on case-sensitive disks too. The collision shows up later
    on Windows if the second spelling is created.
    """

    parent = path.parent
    if not parent.is_dir():
        return
    candidate = path.name
    folded = candidate.casefold()
    for entry in parent.iterdir():
        if entry.name == candidate:
            continue
        if entry.name.casefold() == folded:
            raise CasefoldCollisionError(candidate, entry.name)


def atomic_write_bytes(path, data: bytes) -> None:
    """Write ``data`` to a sibling temp file, close it, then replace ``path``.

    The ``with`` block closes the temp handle before replace. Windows raises
    WinError 32 if it is still open.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("atomic_write_bytes expects bytes")
    payload = bytes(data)
    destination = Path(path)
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(str(parent))
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(str(destination))
    reject_casefold_collision(destination)
    descriptor, temp_name = tempfile.mkstemp(prefix=".part-", suffix=".tmp", dir=parent)
    os.close(descriptor)
    temporary = Path(temp_name)
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
