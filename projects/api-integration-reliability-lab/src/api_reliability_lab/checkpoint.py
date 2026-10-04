"""Durable JSON checkpoints with atomic replace."""

from __future__ import annotations

from typing import Optional
import json
import os
from pathlib import Path

from .errors import CheckpointError
from .models import Checkpoint


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self._data: dict[str, Checkpoint] = {}

    def load(self, sync_id: str) -> Optional[Checkpoint]:
        item = self._data.get(sync_id)
        if item is None:
            return None
        return Checkpoint.from_dict(item.to_dict())

    def save(self, checkpoint: Checkpoint) -> None:
        self._data[checkpoint.sync_id] = Checkpoint.from_dict(checkpoint.to_dict())


class FileCheckpointStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, sync_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in sync_id)
        return self.directory / f"{safe}.json"

    def load(self, sync_id: str) -> Optional[Checkpoint]:
        path = self.path_for(sync_id)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return Checkpoint.from_dict(data)
        except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise CheckpointError(f"corrupt checkpoint {path}: {exc}") from exc

    def save(self, checkpoint: Checkpoint) -> None:
        path = self.path_for(checkpoint.sync_id)
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(checkpoint.to_dict(), indent=2, ensure_ascii=False)
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise CheckpointError(f"could not write checkpoint {path}: {exc}") from exc
