"""Durable JSON checkpoints with atomic replace."""

from __future__ import annotations

from typing import Optional, Protocol
import json
from pathlib import Path

from .errors import CheckpointError
from .models import Checkpoint
from .persist import atomic_write_json, read_json, safe_stem


class CheckpointStore(Protocol):
    def load(self, key: str) -> Optional[Checkpoint]:
        ...

    def save(self, checkpoint: Checkpoint) -> None:
        ...


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self._data: dict[str, Checkpoint] = {}

    def load(self, key: str) -> Optional[Checkpoint]:
        item = self._data.get(key)
        if item is None:
            return None
        return Checkpoint.from_dict(item.to_dict())

    def save(self, checkpoint: Checkpoint) -> None:
        self._data[checkpoint_key(checkpoint.week_id, checkpoint.feed_name)] = Checkpoint.from_dict(
            checkpoint.to_dict()
        )


class FileCheckpointStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        return self.directory / f"{safe_stem(key)}.json"

    def load(self, key: str) -> Optional[Checkpoint]:
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            data = read_json(path)
            return Checkpoint.from_dict(data)
        except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise CheckpointError(f"corrupt checkpoint {path}: {exc}") from exc

    def save(self, checkpoint: Checkpoint) -> None:
        path = self.path_for(checkpoint_key(checkpoint.week_id, checkpoint.feed_name))
        try:
            atomic_write_json(path, checkpoint.to_dict())
        except OSError as exc:
            raise CheckpointError(f"could not write checkpoint {path}: {exc}") from exc


def checkpoint_key(week_id: str, feed_name: str) -> str:
    return f"{week_id}__{safe_stem(feed_name)}"
