"""JSONL compatibility ledger keyed by SHA-256 of the input bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from slip_lab.errors import LedgerConflict, LedgerError
from slip_lab.model import DISPOSITIONS


def sha256(blob: bytes) -> str:
    return hashlib.sha256(bytes(blob)).hexdigest()


def _canonical_row(row: dict) -> dict:
    return json.loads(json.dumps(row, sort_keys=True))


class Ledger:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._index: dict[str, dict] = {}

    def add(self, row: dict) -> dict:
        stored = _canonical_row(row)
        self._validate(stored)
        digest = stored["sha256"]
        previous = self._index.get(digest)
        if previous is not None:
            if previous == stored:
                return previous
            raise LedgerConflict(digest)
        self._index[digest] = stored
        self.rows.append(stored)
        return stored

    def _validate(self, row: dict) -> None:
        outcome = row.get("outcome")
        label = row.get("disposition")
        if outcome != "AGREE" and not label:
            raise LedgerError("non-agreement requires a disposition")
        if label is not None and label not in DISPOSITIONS:
            raise LedgerError(f"unknown disposition {label}")
        if "sha256" not in row or len(str(row["sha256"])) != 64:
            raise LedgerError("sha256 is required")

    def dumps(self) -> str:
        lines = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in self.rows]
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        ledger = cls()
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip():
                continue
            ledger.add(json.loads(line))
        return ledger
