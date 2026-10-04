"""Durable JSON checkpoints with atomic replace."""

from __future__ import annotations

from typing import Optional
import json
from pathlib import Path

from .errors import CheckpointError
from .models import Checkpoint
from .persist import atomic_write_json, safe_stem


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self._data: dict[str, Checkpoint] = {}

    def load(self, job_id: str) -> Optional[Checkpoint]:
        item = self._data.get(job_id)
        if item is None:
            return None
        return Checkpoint.from_dict(item.to_dict())

    def save(self, checkpoint: Checkpoint) -> None:
        self._data[checkpoint.job_id] = Checkpoint.from_dict(checkpoint.to_dict())


class FileCheckpointStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, job_id: str) -> Path:
        return self.directory / f"{safe_stem(job_id)}.checkpoint.json"

    def load(self, job_id: str) -> Optional[Checkpoint]:
        path = self.path_for(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Checkpoint.from_dict(data)
        except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise CheckpointError(f"corrupt checkpoint {path}: {exc}") from exc

    def save(self, checkpoint: Checkpoint) -> None:
        path = self.path_for(checkpoint.job_id)
        try:
            atomic_write_json(path, checkpoint.to_dict())
        except OSError as exc:
            raise CheckpointError(f"could not write checkpoint {path}: {exc}") from exc
