"""Outbox CSV effect. The idempotency key is the receiver's identity.

A peer that ignores the key can still write a second file. This effect
does not: the same key and the same bytes are a replay, and the same key
with different bytes is a conflict.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from shiftlease.errors import EffectConflict
from shiftlease.payload import render_csv


class CsvEffect:
    def __init__(self, outbox: Path) -> None:
        self.outbox = Path(outbox)
        self.calls: list[str] = []
        self.created: list[str] = []

    def path_for(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        return self.outbox / f"{digest}.csv"

    def perform(self, *, idempotency_key: str, payload: dict, fence: int) -> dict:
        del fence  # the store checks the fence; the file identity is the key
        self.calls.append(idempotency_key)
        body = render_csv(idempotency_key, payload)
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.path_for(idempotency_key)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except FileExistsError:
            existing = path.read_bytes()
            if existing == body:
                return {
                    "path": path.name,
                    "bytes": len(body),
                    "replayed": True,
                    "idempotency_key": idempotency_key,
                }
            raise EffectConflict(f"outbox bytes differ for {idempotency_key}") from None
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        self.created.append(idempotency_key)
        return {
            "path": path.name,
            "bytes": len(body),
            "replayed": False,
            "idempotency_key": idempotency_key,
        }
