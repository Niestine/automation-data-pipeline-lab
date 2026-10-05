"""One JSON object per line. Worker lines carry job_id, owner, fence, and seq."""

from __future__ import annotations

import json
import threading
from typing import TextIO


class JsonLogger:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream
        self.records: list[dict] = []
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: object) -> dict:
        record: dict = {"event": event, **fields}
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
        with self._lock:
            self.records.append(record)
            if self.stream is not None:
                self.stream.write(line + "\n")
                self.stream.flush()
        return record
